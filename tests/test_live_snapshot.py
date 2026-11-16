import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from localocr.live_snapshot import inventory, write_stable


class LiveSnapshotTests(unittest.TestCase):
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
            metrics, owner, aliases = inventory(root, now)
            self.assertEqual((metrics["completed_records"], metrics["available_materials"], metrics["available_pages"]), (3, 2, 4))
            self.assertEqual([r["count"] for r in metrics["flags"]], [1, 2])
            self.assertEqual(metrics["recent_7"], 2)
            self.assertEqual((len(owner["artifacts"]), len(aliases)), (2, 1))
            self.assertEqual({a["path"] for a in aliases.values()}, {str(root.resolve())})
            self.assertEqual({r["open"]["source"] for r in owner["artifacts"]}, set(aliases))
            self.assertEqual({r["open"]["path"] for r in owner["artifacts"]}, {"0.md", "2.md"})
            path = root / "snapshot.json"
            write_stable(path, dict(at="first", metrics=metrics))
            before = path.stat().st_mtime_ns
            self.assertEqual(write_stable(path, dict(at="later", metrics=metrics))["at"], "first")
            self.assertEqual(path.stat().st_mtime_ns, before)
