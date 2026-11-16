"""Read retained job metadata for the website; never run OCR or load models."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .retained_results import read_latest_complete_results


def physical_path(value):
    match = re.match(r"/mnt/([a-z])/(.*)", value) if os.name == "nt" else None
    return Path(f"{match[1].upper()}:/{match[2]}" if match else value)


def inventory(data_root, now):
    if not (data_root / "_server/jobs").is_dir():
        raise FileNotFoundError("retained job directory is unavailable")
    rows, states = [], Counter()
    def normalize(path, job):
        states[job.get("status", "missing")] += 1
        source = job.get("source_path", "").replace("\\", "/").lower()
        if job.get("status") != "completed" or any(s in source for s in (
                "/tests/samples/", "/localocr/inputs/", "smoke", "/media-empty-acceptance/blank.png")):
            return None
        files = job.get("output_files", {})
        row = dict(source_id=job["source_sha256"], at=job.get("updated_at"), job=job,
                   complete=all(files.get(k) and physical_path(files[k]).is_file()
                                for k in ("txt", "md", "json", "objective")))
        rows.append(row)
        return row
    latest = read_latest_complete_results((data_root / "_server/jobs").glob("*.json"), normalize)
    available = [r for r in rows if r["complete"]]
    auto = [r for r in rows if r["job"].get("result", {}).get("route", {}).get("requested_engine") == "auto"]
    fast = sum(r["job"].get("result", {}).get("model_id") == "ppocrv6-medium" for r in auto)
    metrics = dict(source_materials=len({r["source_id"] for r in rows}), available_materials=len(latest),
        completed_records=len(rows), available_results=len(available),
        processed_pages=sum(len(r["job"].get("result", {}).get("pages", [])) for r in rows),
        available_pages=sum(len(r["job"].get("result", {}).get("pages", [])) for r in latest.values()),
        latest_completed=max((r["at"] for r in rows if r["at"]), key=lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")), default=None),
        cpu_results=sum(r["job"].get("result", {}).get("device") == "cpu" for r in rows))
    for days in (7, 30):
        recent = [r for r in rows if r["at"] and now-timedelta(days=days) <= datetime.fromisoformat(r["at"].replace("Z", "+00:00")) <= now]
        metrics[f"recent_{days}"] = len({r["source_id"] for r in recent})
        metrics[f"recent_records_{days}"] = len(recent)
    metrics["escalated"] = dict(total=len(auto), strong=len(auto)-fast,
        text=f"{len(auto)-fast:,} 次／共 {len(auto):,} 次" + (f"（{(len(auto)-fast)/len(auto):.0%}）" if auto else ""))
    metrics["auto_fast_percent"] = round(fast/len(auto)*100, 2) if auto else None
    metrics["job_states"] = dict(states, total=sum(states.values()), text="；".join(f"{k}：{v:,} 份" for k, v in sorted(states.items())))
    quality = Counter(r["job"].get("result", {}).get("quality", {}).get("status", "missing") for r in latest.values())
    metrics.update(low_confidence=quality["low_confidence"], unknown=quality["unknown"], sufficient=quality["sufficient"])
    groups = dict(types=Counter(Path(r["job"]["source_path"]).suffix.lower() for r in latest.values()),
        flags=Counter(flag for r in latest.values()
                      for flag in set(r["job"].get("result", {}).get("quality", {}).get("flags", [])) if flag != "low_confidence"))
    names = dict(empty_observation="没读到字，不能证明无字", confidence_unavailable="模型没给置信分",
                 escalation_skipped_gpu_unavailable="显卡不可用，更强一轮没跑起来")
    for key, counts in groups.items():
        metrics[key] = [dict(label=names.get(label, label), count=count, text=f"{names.get(label, label)}：{count:,} 份",
                             state_text="机器质量标记" if key != "types" else "四种识别稿仍在") for label, count in sorted(counts.items())]
    public, named, artifacts, aliases = [], [], [], {}
    for index, row in enumerate(sorted(latest.values(), key=lambda r: datetime.fromisoformat(r["at"].replace("Z", "+00:00")), reverse=True)[:20], 1):
        job = row["job"]
        kind = Path(job["source_path"]).suffix.lower().lstrip(".")
        public.append(dict(at=row["at"], kind=kind, text=f"识别稿 {index:02d} · {kind}", state_text="四种识别稿仍在"))
        named.append(dict(at=row["at"], text=job["source_path"].replace("\\", "/").rsplit("/", 1)[-1],
            pages=len(job.get("result", {}).get("pages", [])), model=job.get("result", {}).get("model_id"),
            state_text=job.get("result", {}).get("quality", {}).get("status", "未记录状态")))
        output = physical_path(job["output_files"]["md"]).resolve()
        directory = output.parent
        alias = "ocr.results."+hashlib.sha256(directory.as_posix().casefold().encode("utf-8")).hexdigest()[:12]
        aliases[alias] = dict(path=str(directory), format="exists")
        artifacts.append(dict(at=row["at"], title=named[-1]["text"], kind="ocr", status="completed", public="owner",
                              open=dict(source=alias, path=output.relative_to(directory).as_posix())))
    metrics["recent"] = public
    return metrics, dict(metrics=dict(recent_named=named), artifacts=artifacts), aliases


def runtime_inventory():
    script = '''import json,re,subprocess
from pathlib import Path
from datetime import datetime,timezone
r=Path('/root/localocr-runtimes/current'); m=Path('/root/.paddlex/official_models')
a=json.loads((r/'acceptance/acceptance.json').read_text())
q=json.loads((r/'acceptance/quality.json').read_text())
u=(r/'acceptance/unit.log').read_text()
print(json.dumps(dict(
    model_components=sum(p.is_dir() for p in m.iterdir()),
    models_bytes=int(subprocess.check_output(['du','-sb',str(m)],text=True).split()[0]),
    runtime=r.resolve().name,
    acceptance_at=datetime.fromtimestamp(a['started_unix']+a['elapsed_sec'],timezone.utc).isoformat(),
    tests=int(re.search(r'Ran (\\d+) tests',u)[1]),
    skipped=int(re.search(r'OK \\(skipped=(\\d+)\\)',u)[1]),
    quality_cases=len(q['cases']), quality_passed=sum(c['ok'] is True for c in q['cases']),
    model_records=[dict(text=p.name,state_text='已下载') for p in sorted(m.iterdir()) if p.is_dir()],
    quality_records=[dict(text=c['name'],cer=c.get('cer'),state_text='通过' if c['ok'] else '未通过')
                     for c in q['cases']]
)))'''
    return json.loads(subprocess.run(["wsl.exe", "-d", "Ubuntu", "-e", "python3", "-B", "-"], input=script,
        text=True, encoding="utf-8", capture_output=True, check=True, timeout=60).stdout)


def write_stable(path, payload):
    """Unchanged values keep their source time and file bytes, including LF."""
    previous = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    if {k: v for k, v in previous.items() if k != "at"} == {k: v for k, v in payload.items() if k != "at"}:
        return previous
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    for name in ("output", "runtime-output", "owner-output", "sources-output"):
        parser.add_argument("--"+name, type=Path, required=True)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    metrics, owner, aliases = inventory(args.data_root, now)
    root = Path(__file__).resolve().parents[1]
    size = dict(docs_chars=0, code_lines=0)
    for name in subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], encoding="utf-8").split("\0"):
        path = root / name
        if path.suffix.lower() not in {".md", ".py", ".ps1", ".sh", ".bat"}:
            continue
        text = path.read_text(encoding="utf-8-sig")
        size["docs_chars" if path.suffix.lower() == ".md" else "code_lines"] += sum(not c.isspace() for c in text) if path.suffix.lower() == ".md" else len(text.splitlines())
    scope = "现存完成记录；排明确样例；含 AI 批量截图。完整只核对四种文件存在，不代表人工核准或哈希复验。"
    old_size = json.loads(args.output.read_text(encoding="utf-8-sig")).get("size", {}) if args.output.is_file() else {}
    size["at"] = old_size.get("at") if {k: v for k, v in old_size.items() if k != "at"} == size else now.isoformat()
    snapshot = write_stable(args.output, dict(v=1, at=now.isoformat(), scope=scope, metrics=metrics, size=size, artifacts=[]))
    write_stable(args.owner_output, dict(v=1, at=snapshot["at"], scope=scope, **owner))
    runtime = runtime_inventory()
    runtime["models"] = dict(bytes=runtime["models_bytes"], components=runtime["model_components"],
        text=f"{runtime['models_bytes']/2**30:.2f} GB，{runtime['model_components']} 个组件")
    write_stable(args.runtime_output, dict(v=1, at=now.isoformat(), scope="已安装模型逻辑大小；已激活版本的既有验收，不运行识别", metrics=runtime))
    write_stable(args.sources_output, dict(sources=aliases))


if __name__ == "__main__":
    main()
