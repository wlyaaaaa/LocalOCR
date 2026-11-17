import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from localocr.live_snapshot import inventory, main, write_stable


class LiveSnapshotTests(unittest.TestCase):
    def test_incremental_jobs_and_live_output_availability(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "_server/jobs"
            jobs.mkdir(parents=True)
            files = {kind: str(root / kind) for kind in ("txt", "md", "json", "objective")}
            for file in files.values(): Path(file).write_text("synthetic")
            job = dict(status="completed", source_path="synthetic.png", source_sha256="a",
                updated_at=now.isoformat(), output_files=files, result=dict(pages=[dict(text="not-cached")]))
            path, cache = jobs / "one.json", root / "cache.json"
            path.write_text(json.dumps(job))
            first = inventory(root, now, cache)
            self.assertNotIn("not-cached", cache.read_text())
            with patch("localocr.live_snapshot.json.loads", wraps=json.loads) as loads:
                self.assertEqual(inventory(root, now, cache), first)
                self.assertEqual(loads.call_count, 1)
            added = jobs / "two.json"
            added.write_text(json.dumps(dict(job, source_sha256="b")))
            self.assertEqual(inventory(root, now, cache)[0]["job_states"]["total"], 2)
            added.unlink()
            job["result"]["pages"].append({})
            path.write_text(json.dumps(job))
            self.assertEqual(inventory(root, now, cache)[0]["processed_pages"], 2)
            Path(files["objective"]).unlink()
            self.assertEqual(inventory(root, now, cache)[0]["available_materials"], 0)
            path.unlink()
            self.assertEqual(inventory(root, now, cache)[0]["job_states"]["total"], 0)

    def test_latest_complete_flags_overlap_and_stable_time(self):
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "_server/jobs").mkdir(parents=True)
            for index, source in enumerate(("a", "a", "b")):
                files = {}
                for kind in ("txt", "md", "json", "objective"):
                    path = root / f"{index}.{kind}"
                    if index != 1 or kind != "objective": path.write_text("synthetic")
                    files[kind] = str(path)
                result = dict(pages=[{}]*(index+1), quality=dict(status="unknown",
                    flags=["empty_observation"] + (["confidence_unavailable"] if index == 2 else [])))
                job = dict(status="completed", source_path="synthetic.png", source_sha256=source,
                    updated_at=(now-timedelta(hours=3-index)).isoformat(), output_files=files, result=result)
                (root / f"_server/jobs/{index}.json").write_text(json.dumps(job))
            for index, job in enumerate((dict(status="failed"), dict(status="running"), {}, dict(status="other")), 3):
                (root / f"_server/jobs/{index}.json").write_text(json.dumps(job))
            metrics, owner, aliases = inventory(root, now)
            self.assertEqual(metrics["job_states"], dict(
                completed=3, failed=1, running=1, missing=1, other=1, total=7,
                text="完成：3 份；失败：1 份；未记录状态：1 份；未知状态：1 份；运行中：1 份"))
            self.assertEqual(metrics["latest_completed"], (now-timedelta(hours=1)).isoformat())
            self.assertEqual((metrics["completed_records"], metrics["available_materials"], metrics["available_pages"]), (3, 2, 4))
            self.assertEqual([r["count"] for r in metrics["flags"]], [1, 2])
            self.assertEqual(metrics["recent_7"], 2)
            self.assertEqual((len(owner["artifacts"]), len(aliases)), (2, 1))
            self.assertEqual({r["state_text"] for r in owner["metrics"]["recent_named"]}, {"未给质量结论"})
            self.assertEqual({r["status"] for r in owner["artifacts"]}, {"completed"})
            self.assertEqual({a["path"] for a in aliases.values()}, {str(root.resolve())})
            self.assertEqual({r["open"]["source"] for r in owner["artifacts"]}, set(aliases))
            self.assertEqual({r["open"]["path"] for r in owner["artifacts"]}, {"0.md", "2.md"})
            path = root / "snapshot.json"
            write_stable(path, dict(at="first", metrics=metrics))
            before = path.stat().st_mtime_ns
            self.assertEqual(write_stable(path, dict(at="later", metrics=metrics))["at"], "first")
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_cli_rebuilds_truncated_snapshots_from_retained_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "_server/jobs"
            jobs.mkdir(parents=True)
            job = dict(status="completed", source_path="synthetic.png", source_sha256="a", result=dict(pages=[{}]))
            (jobs / "one.json").write_text(json.dumps(job), encoding="utf-8")
            paths = {key: root / f"{key}.json"
                     for key in ("output", "runtime-output", "owner-output", "sources-output")}
            args = ["live_snapshot", "--data-root", directory]
            for key, path in paths.items():
                args.extend([f"--{key}", str(path)])
                path.write_bytes(b'{"v": ')
            cache = paths["output"].with_suffix(".inventory-cache.json")
            runtime = dict(models_bytes=1, model_components=1)
            with (patch("sys.argv", args), patch("localocr.live_snapshot.subprocess.check_output", return_value=""),
                  patch("localocr.live_snapshot.runtime_inventory", return_value=runtime)):
                main()
                cache.write_bytes(b'{"v": ')
                main()
            snapshots = {key: json.loads(path.read_bytes()) for key, path in paths.items()}
            self.assertEqual(snapshots["output"]["metrics"]["processed_pages"], 1)
            self.assertEqual(snapshots["output"]["size"]["at"], snapshots["output"]["at"])
            self.assertIn("one.json", json.loads(cache.read_bytes())["jobs"])

    def test_failed_snapshot_replace_keeps_old_bytes_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            write_stable(path, dict(at="first", metrics={"total": 1}))
            before = path.read_bytes()
            payload = dict(at="later", metrics={"total": 2})
            with patch.object(Path, "replace", autospec=True, side_effect=PermissionError("file in use")) as replace:
                with self.assertRaisesRegex(PermissionError, "file in use"):
                    write_stable(path, payload)
            self.assertEqual(replace.call_args.args[0].parent, path.parent)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])
            write_stable(path, payload)
            self.assertEqual(json.loads(path.read_bytes()), payload)
