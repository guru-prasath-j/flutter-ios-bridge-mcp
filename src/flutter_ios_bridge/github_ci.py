"""Build iOS on GitHub-hosted macOS runners and pull the results back to Windows/Linux."""

import base64
import configparser
import io
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Optional

import httpx
from nacl import encoding, public

from .config import Config, github_token, load_config
from .errors import BridgeError
from .project import resolve_project

API = "https://api.github.com"
ERROR_PATTERNS = re.compile(
    r"(error:|Error:|ERROR|\bfailed\b|FAILED|Exception|❌|Could not|No such module|"
    r"No profiles for|Provisioning profile|Signing for|requires a development team|CocoaPods|"
    r"pod install|Undefined symbol|ld: |Unhandled|The following build commands failed|##\[error\])"
)
TS_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


# ---------------------------------------------------------------- repo discovery

def git_root(start: Path) -> Optional[Path]:
    for cand in [start, *start.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def repo_from_git(root: Path) -> Optional[str]:
    cfgfile = root / ".git" / "config"
    if not cfgfile.is_file():
        return None
    cp = configparser.ConfigParser(strict=False)
    try:
        cp.read(cfgfile, encoding="utf-8")
    except configparser.Error:
        return None
    url = cp.get('remote "origin"', "url", fallback=None)
    if not url:
        return None
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


class GitHub:
    def __init__(self, cfg: Optional[Config] = None, project_path: Optional[str] = None, repo: Optional[str] = None):
        self.cfg = cfg or load_config()
        tok = github_token(self.cfg)
        if not tok:
            raise BridgeError("No GitHub token. Set GITHUB_TOKEN, add [github].token to the config, or run `gh auth login`.")
        self.project_root: Optional[Path] = None
        self.git_root: Optional[Path] = None
        try:
            self.project_root = resolve_project(project_path)
            self.git_root = git_root(self.project_root)
        except BridgeError:
            if not (repo or self.cfg.github.repo):
                raise
        self.repo = repo or self.cfg.github.repo or (repo_from_git(self.git_root) if self.git_root else None)
        if not self.repo:
            raise BridgeError("Can't tell which GitHub repo to use. Push the project to GitHub or set FIB_GITHUB_REPO=owner/name.")
        self.workflow = self.cfg.github.workflow
        self.http = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {tok}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "flutter-ios-bridge-mcp",
            },
            timeout=60,
            follow_redirects=True,
        )

    # -- http helpers
    def _req(self, method: str, url: str, **kw) -> httpx.Response:
        r = self.http.request(method, url, **kw)
        if r.status_code >= 400:
            msg = r.text[:500]
            hint = ""
            if r.status_code == 404:
                hint = " (repo/workflow not found, or the token can't see it — is the workflow committed and pushed?)"
            elif r.status_code in (401, 403):
                hint = " (token lacks permission: needs Actions + Secrets read/write, Contents read)"
            raise BridgeError(f"GitHub API {method} {url} -> {r.status_code}{hint}: {msg}")
        return r

    def get(self, url: str, **kw) -> Any:
        return self._req("GET", url, **kw).json()

    # -- repo
    def default_branch(self) -> str:
        return self.get(f"/repos/{self.repo}")["default_branch"]

    # -- secrets
    def set_secret(self, name: str, value: str) -> None:
        key = self.get(f"/repos/{self.repo}/actions/secrets/public-key")
        box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
        enc = base64.b64encode(box.encrypt(value.encode())).decode()
        self._req("PUT", f"/repos/{self.repo}/actions/secrets/{name}",
                  json={"encrypted_value": enc, "key_id": key["key_id"]})

    def list_secrets(self) -> list[str]:
        return [s["name"] for s in self.get(f"/repos/{self.repo}/actions/secrets")["secrets"]]

    # -- runs
    def dispatch(self, ref: str, inputs: dict[str, Any]) -> Optional[int]:
        r = self._req("POST", f"/repos/{self.repo}/actions/workflows/{self.workflow}/dispatches",
                      json={"ref": ref, "inputs": inputs, "return_run_details": True})
        if r.status_code == 200 and r.content:
            try:
                return int(r.json().get("workflow_run_id"))
            except Exception:
                return None
        return None

    def runs(self, limit: int = 10, branch: Optional[str] = None) -> list[dict]:
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if branch:
            params["branch"] = branch
        data = self.get(f"/repos/{self.repo}/actions/workflows/{self.workflow}/runs", params=params)
        return data.get("workflow_runs", [])

    def run(self, run_id: int) -> dict:
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}")

    def jobs(self, run_id: int) -> list[dict]:
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}/jobs", params={"per_page": 50})["jobs"]

    def job_log(self, job_id: int) -> str:
        return self._req("GET", f"/repos/{self.repo}/actions/jobs/{job_id}/logs").text

    def artifacts(self, run_id: int) -> list[dict]:
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}/artifacts")["artifacts"]

    def download(self, url: str) -> bytes:
        return self._req("GET", url).content

    def cancel(self, run_id: int) -> None:
        self._req("POST", f"/repos/{self.repo}/actions/runs/{run_id}/cancel")


# ---------------------------------------------------------------- summaries

def summarize_run(run: dict) -> dict:
    return {
        "id": run["id"],
        "title": run.get("display_title") or run.get("name"),
        "status": run["status"],
        "conclusion": run.get("conclusion"),
        "branch": run.get("head_branch"),
        "commit": (run.get("head_sha") or "")[:7],
        "created_at": run.get("created_at"),
        "url": run.get("html_url"),
    }


def summarize_jobs(jobs: list[dict]) -> list[dict]:
    out = []
    for j in jobs:
        out.append({
            "id": j["id"],
            "name": j["name"],
            "status": j["status"],
            "conclusion": j.get("conclusion"),
            "steps": [
                {"name": s["name"], "status": s["status"], "conclusion": s.get("conclusion")}
                for s in j.get("steps", [])
            ],
        })
    return out


def extract_errors(log: str, tail_lines: int = 120, max_errors: int = 80) -> dict:
    lines = [TS_PREFIX.sub("", ln) for ln in log.splitlines()]
    # map each line to its step (steps start with "##[group]Run ...")
    errors: list[str] = []
    seen: set[str] = set()
    for i, ln in enumerate(lines):
        if ERROR_PATTERNS.search(ln) and not ln.startswith("##[group]") and "echo \"::error" not in ln:
            key = ln.strip()
            if key and key not in seen:
                seen.add(key)
                errors.append(ln.rstrip())
    return {
        "error_lines": errors[-max_errors:],
        "tail": "\n".join(lines[-tail_lines:]),
    }


# ---------------------------------------------------------------- workflow generation

def render_workflow(project_dir_rel: str, flutter_version: Optional[str], runner: str) -> str:
    tpl = resources.files("flutter_ios_bridge").joinpath("templates/ios_workflow.yml").read_text(encoding="utf-8")
    fv_line = f"          flutter-version: \"{flutter_version}\"\n" if flutter_version else ""
    return (tpl.replace("__RUNNER__", runner)
               .replace("__PROJECT_DIR__", project_dir_rel or ".")
               .replace("__FLUTTER_VERSION_LINE__", fv_line))


def signing_script_source() -> str:
    return resources.files("flutter_ios_bridge").joinpath("pbxproj_signing.py").read_text(encoding="utf-8")


def detect_local_flutter_version() -> Optional[str]:
    exe = shutil.which("flutter")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version", "--machine"], capture_output=True, text=True, timeout=60,
                             shell=False).stdout
        m = re.search(r'"frameworkVersion"\s*:\s*"([^"]+)"', out)
        return m.group(1) if m else None
    except Exception:
        return None


def generate_workflow(project_path: Optional[str], flutter_version: Optional[str], runner: str,
                      overwrite: bool, match_local_flutter: bool) -> dict:
    root = resolve_project(project_path)
    groot = git_root(root) or root
    rel = root.relative_to(groot).as_posix()
    if not flutter_version and match_local_flutter:
        flutter_version = detect_local_flutter_version()
    wf = groot / ".github" / "workflows" / load_config().github.workflow
    script = groot / ".github" / "scripts" / "ios_manual_signing.py"
    if wf.exists() and not overwrite:
        raise BridgeError(f"{wf} already exists. Pass overwrite=true to regenerate.")
    wf.parent.mkdir(parents=True, exist_ok=True)
    script.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(render_workflow(rel if rel != "." else ".", flutter_version, runner), encoding="utf-8")
    script.write_text(signing_script_source(), encoding="utf-8")
    notes = []
    if not (groot / ".git").exists():
        notes.append("This folder is not a git repo yet: `git init`, create a GitHub repo, and push.")
    notes.append("Commit and push .github/ to the default branch — GitHub only runs workflows it can see there.")
    return {
        "workflow_file": str(wf),
        "signing_script": str(script),
        "project_dir_in_repo": rel or ".",
        "flutter_version": flutter_version or "latest stable",
        "runner": runner,
        "next_steps": notes + [
            "Unsigned/simulator builds work immediately (free macOS minutes on public repos; private repos use your Actions quota at a 10x multiplier).",
            "For signed builds: run signing_quick_setup to create certs/profiles and upload the secrets.",
        ],
    }


# ---------------------------------------------------------------- local git hygiene

def local_git_warnings(groot: Optional[Path], ref: str) -> list[str]:
    if not groot or not shutil.which("git"):
        return []
    warns = []
    try:
        st = subprocess.run(["git", "-C", str(groot), "status", "--porcelain"], capture_output=True, text=True, timeout=20).stdout
        if st.strip():
            warns.append(f"You have {len(st.strip().splitlines())} uncommitted change(s); CI builds what is pushed to '{ref}', not your working copy.")
        subprocess.run(["git", "-C", str(groot), "fetch", "-q", "origin", ref], capture_output=True, timeout=30)
        ahead = subprocess.run(["git", "-C", str(groot), "rev-list", "--count", f"origin/{ref}..HEAD"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
        if ahead and ahead != "0":
            warns.append(f"{ahead} local commit(s) not pushed to origin/{ref}.")
    except Exception:
        pass
    return warns


# ---------------------------------------------------------------- high-level ops

def trigger(project_path: Optional[str], build_type: str, export_method: str, testflight: bool,
            ref: Optional[str], build_name: str, build_number: str, repo: Optional[str]) -> dict:
    if build_type not in ("simulator", "unsigned-ipa", "signed-ipa"):
        raise BridgeError("build_type must be simulator, unsigned-ipa or signed-ipa")
    gh = GitHub(project_path=project_path, repo=repo)
    ref = ref or gh.cfg.github.ref or gh.default_branch()
    warnings = local_git_warnings(gh.git_root, ref)
    if build_type == "signed-ipa":
        have = set(gh.list_secrets())
        missing = [s for s in ("IOS_P12_BASE64", "IOS_P12_PASSWORD", "IOS_PROFILE_BASE64") if s not in have]
        if testflight:
            missing += [s for s in ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY") if s not in have]
        if missing:
            raise BridgeError(f"Repo secrets missing for a signed build: {missing}. Run signing_quick_setup first.")
    req_id = uuid.uuid4().hex[:8]
    started = datetime.now(timezone.utc)
    run_id = gh.dispatch(ref, {
        "build_type": build_type, "export_method": export_method, "testflight": testflight,
        "build_name": build_name, "build_number": build_number, "request_id": req_id,
    })
    run = None
    if run_id:
        run = gh.run(run_id)
    else:
        for _ in range(20):
            time.sleep(3)
            for r in gh.runs(limit=15):
                if req_id in (r.get("display_title") or ""):
                    run = r
                    break
            if run:
                break
    return {
        "repo": gh.repo,
        "ref": ref,
        "request_id": req_id,
        "run": summarize_run(run) if run else None,
        "note": None if run else f"Dispatched at {started.isoformat()} but the run hasn't appeared yet; use ci_list_runs.",
        "warnings": warnings,
        "next": "Call ci_wait_for_run with the run id, then ci_download_artifacts (or ci_get_logs if it fails).",
    }


def wait_for_run(run_id: int, timeout_s: int, poll_s: int, project_path: Optional[str], repo: Optional[str]) -> dict:
    gh = GitHub(project_path=project_path, repo=repo)
    deadline = time.monotonic() + max(10, timeout_s)
    run = gh.run(run_id)
    while run["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(max(5, poll_s))
        run = gh.run(run_id)
    out = {"run": summarize_run(run), "jobs": summarize_jobs(gh.jobs(run_id))}
    if run["status"] != "completed":
        out["note"] = "Still running — call ci_wait_for_run again."
    elif run.get("conclusion") == "failure":
        out["note"] = "Build failed — call ci_get_logs for the error lines."
    return out


def logs(run_id: int, only_failed: bool, tail_lines: int, project_path: Optional[str], repo: Optional[str]) -> dict:
    gh = GitHub(project_path=project_path, repo=repo)
    result = []
    for j in gh.jobs(run_id):
        if only_failed and j.get("conclusion") not in ("failure", "cancelled", "timed_out"):
            continue
        failed_steps = [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"]
        try:
            text = gh.job_log(j["id"])
        except BridgeError as e:
            result.append({"job": j["name"], "error": str(e)})
            continue
        result.append({"job": j["name"], "conclusion": j.get("conclusion"), "failed_steps": failed_steps,
                       **extract_errors(text, tail_lines=tail_lines)})
    if not result:
        return {"note": "No failed jobs in this run (pass only_failed=false for full logs).", "jobs": []}
    return {"jobs": result}


def safe_extract(zf: zipfile.ZipFile, target: Path) -> None:
    root = target.resolve()
    for member in zf.infolist():
        dest = (root / member.filename).resolve()
        if dest != root and root not in dest.parents:
            raise BridgeError(f"Refusing to extract suspicious path {member.filename!r}")
        zf.extract(member, root)


def download_artifacts(run_id: int, dest_dir: Optional[str], project_path: Optional[str], repo: Optional[str]) -> dict:
    gh = GitHub(project_path=project_path, repo=repo)
    arts = [a for a in gh.artifacts(run_id) if not a.get("expired")]
    if not arts:
        raise BridgeError("This run has no (unexpired) artifacts. Did it succeed?")
    base = Path(dest_dir).expanduser() if dest_dir else (
        (gh.project_root / "build" / "ios-bridge" / str(run_id)) if gh.project_root else Path.cwd() / f"ios-bridge-{run_id}")
    files = []
    for a in arts:
        target = base / a["name"]
        target.mkdir(parents=True, exist_ok=True)
        blob = gh.download(a["archive_download_url"])
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            safe_extract(zf, target)
        for f in target.rglob("*"):
            if f.is_file():
                files.append({"path": str(f), "bytes": f.stat().st_size})
    hints = []
    if any(f["path"].endswith("unsigned.ipa") for f in files):
        hints.append("Runner-unsigned.ipa can be sideloaded from Windows/Linux with Sideloadly/AltStore/zsign using a free Apple ID, "
                     "or re-signed later.")
    if any(f["path"].endswith(".ipa") and "unsigned" not in f["path"] for f in files):
        hints.append("Install the signed IPA on a USB-connected iPhone with device_install_app (ad-hoc/development builds only).")
    if any("simulator" in f["path"] for f in files):
        hints.append("Simulator .app runs on a Mac only: use mac_install_simulator_app if a remote Mac is configured.")
    return {"dest": str(base), "files": files, "hints": hints}
