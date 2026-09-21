"""Drive a real Mac over SSH: sync the project, build, run on the Simulator, grab screenshots/logs."""

import io
import json
import posixpath
import shlex
import tarfile
import time
from pathlib import Path
from typing import Optional

import paramiko

from .config import Config, load_config
from .errors import BridgeError
from .project import main_bundle_id, resolve_project

EXCLUDE_DIRS = {".git", ".dart_tool", "build", ".idea", ".gradle", "Pods", ".symlinks", "ephemeral",
                ".fvm", "node_modules", ".vscode", "DerivedData", ".cxx"}
EXCLUDE_FILES = {".flutter-plugins", ".flutter-plugins-dependencies", "local.properties", "Podfile.lock.bak"}


class Mac:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or load_config()
        m = self.cfg.mac
        if not m.configured:
            raise BridgeError("No remote Mac configured. Set FIB_MAC_HOST and FIB_MAC_USER (plus FIB_MAC_KEY_PATH or "
                              "FIB_MAC_PASSWORD). Any Mac with Xcode + Flutter and Remote Login enabled works "
                              "(System Settings → General → Sharing → Remote Login).")
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(m.host, port=m.port, username=m.user,
                                key_filename=str(Path(m.key_path).expanduser()) if m.key_path else None,
                                password=m.password, timeout=20, allow_agent=True, look_for_keys=not m.key_path)
        except Exception as e:
            raise BridgeError(f"SSH to {m.user}@{m.host}:{m.port} failed: {e}")
        self._home: Optional[str] = None

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------ exec
    def run(self, command: str, cwd: Optional[str] = None, timeout: int = 900, check: bool = False) -> dict:
        full = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        # login shell so PATH includes flutter, pod, xcrun from the user's profile
        wrapped = f"{self.cfg.mac.shell} -lc {shlex.quote(full)}"
        start = time.monotonic()
        stdin, stdout, stderr = self.client.exec_command(wrapped, timeout=timeout, get_pty=False)
        stdin.close()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        res = {"exit_code": code, "stdout": out, "stderr": err, "seconds": round(time.monotonic() - start, 1)}
        if check and code != 0:
            raise BridgeError(f"`{command}` failed on the Mac (exit {code}):\n{_tail(out + err, 80)}")
        return res

    def home(self) -> str:
        if not self._home:
            self._home = self.run("printf %s \"$HOME\"", check=True)["stdout"].strip()
        return self._home

    def expand(self, path: str) -> str:
        return self.home() + path[1:] if path.startswith("~") else path

    def project_dir(self, project_name: str) -> str:
        return posixpath.join(self.expand(self.cfg.mac.remote_dir), project_name)

    # ------------------------------------------------ files
    def upload_bytes(self, data: bytes, remote_path: str) -> None:
        with self.client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(data), remote_path)

    def download(self, remote_path: str, local_path: Path) -> Path:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with self.client.open_sftp() as sftp:
            sftp.get(self.expand(remote_path), str(local_path))
        return local_path

    def read_bytes(self, remote_path: str) -> bytes:
        buf = io.BytesIO()
        with self.client.open_sftp() as sftp:
            sftp.getfo(self.expand(remote_path), buf)
        return buf.getvalue()


def _tail(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def _trim(res: dict, n: int = 150) -> dict:
    return {**res, "stdout": _tail(res["stdout"], n), "stderr": _tail(res["stderr"], n)}


def pack_project(root: Path) -> tuple[bytes, int]:
    buf = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in EXCLUDE_DIRS for part in rel.parts) or p.name in EXCLUDE_FILES:
                continue
            if p.is_file():
                tar.add(p, arcname=rel.as_posix(), recursive=False)
                count += 1
    return buf.getvalue(), count


# ======================================================== operations

def sync(project_path: Optional[str], clean: bool = False) -> dict:
    root = resolve_project(project_path)
    blob, n = pack_project(root)
    with Mac() as mac:
        dest = mac.project_dir(root.name)
        tmp = f"/tmp/fib-{int(time.time())}.tar.gz"
        mac.upload_bytes(blob, tmp)
        # keep ios/Pods and build/ between syncs for speed unless clean
        prep = f"rm -rf {shlex.quote(dest)} && " if clean else ""
        mac.run(f"{prep}mkdir -p {shlex.quote(dest)} && tar -xzf {tmp} -C {shlex.quote(dest)} && rm -f {tmp}", check=True)
    return {"remote_dir": dest, "files": n, "bytes": len(blob), "clean": clean}


def doctor() -> dict:
    with Mac() as mac:
        res = mac.run("sw_vers; echo; xcodebuild -version; echo; flutter --version; echo; pod --version; echo; "
                      "xcrun simctl list runtimes | tail -n 5", timeout=180)
    return _trim(res, 60)


def exec_command(command: str, in_project: bool, project_path: Optional[str], timeout: int) -> dict:
    name = resolve_project(project_path).name if in_project else None
    with Mac() as mac:
        cwd = mac.project_dir(name) if name else None
        return _trim(mac.run(command, cwd=cwd, timeout=timeout))


def build(project_path: Optional[str], mode: str, sync_first: bool, extra_args: str) -> dict:
    cmds = {
        "simulator": "flutter build ios --simulator --debug",
        "no-codesign": "flutter build ios --release --no-codesign",
        "ipa": "flutter build ipa --release",
    }
    if mode not in cmds:
        raise BridgeError(f"mode must be one of {list(cmds)}")
    root = resolve_project(project_path)
    synced = sync(str(root)) if sync_first else None
    with Mac() as mac:
        d = mac.project_dir(root.name)
        res = mac.run(f"flutter pub get >/dev/null && {cmds[mode]} {extra_args}".strip(), cwd=d, timeout=3600)
    out = {"mode": mode, "remote_dir": d, "synced": synced, **_trim(res, 120)}
    out["ok"] = res["exit_code"] == 0
    return out


def list_simulators(only_booted: bool = False) -> list[dict]:
    with Mac() as mac:
        raw = mac.run("xcrun simctl list devices available -j", check=True)["stdout"]
    sims = []
    for runtime, devs in json.loads(raw)["devices"].items():
        if "iOS" not in runtime:
            continue
        rt = runtime.rsplit(".", 1)[-1].replace("iOS-", "iOS ").replace("-", ".")
        for dv in devs:
            if only_booted and dv["state"] != "Booted":
                continue
            sims.append({"name": dv["name"], "udid": dv["udid"], "state": dv["state"], "runtime": rt})
    return sims


def _pick_sim(mac: Mac, device: Optional[str]) -> str:
    raw = json.loads(mac.run("xcrun simctl list devices available -j", check=True)["stdout"])
    all_devs = [dv for rt, ds in raw["devices"].items() if "iOS" in rt for dv in ds]
    if device:
        for dv in all_devs:
            if device in (dv["udid"], dv["name"]):
                return dv["udid"]
        raise BridgeError(f"No available simulator named/udid {device!r}")
    booted = [dv for dv in all_devs if dv["state"] == "Booted"]
    if booted:
        return booted[0]["udid"]
    iphones = [dv for dv in all_devs if dv["name"].startswith("iPhone")]
    if not iphones:
        raise BridgeError("No iPhone simulators installed on the Mac (Xcode → Settings → Platforms).")
    return iphones[-1]["udid"]


def boot(device: Optional[str]) -> dict:
    with Mac() as mac:
        udid = _pick_sim(mac, device)
        mac.run(f"xcrun simctl boot {udid} 2>/dev/null; xcrun simctl bootstatus {udid} -b", timeout=300)
        mac.run("open -a Simulator || true")
    return {"booted": udid}


def run_on_simulator(project_path: Optional[str], device: Optional[str], sync_first: bool, rebuild: bool) -> dict:
    root = resolve_project(project_path)
    bundle = main_bundle_id(root)
    if sync_first:
        sync(str(root))
    with Mac() as mac:
        d = mac.project_dir(root.name)
        udid = _pick_sim(mac, device)
        mac.run(f"xcrun simctl boot {udid} 2>/dev/null; xcrun simctl bootstatus {udid} -b", timeout=300)
        build_res = None
        if rebuild:
            build_res = mac.run("flutter pub get >/dev/null && flutter build ios --simulator --debug", cwd=d, timeout=3600)
            if build_res["exit_code"] != 0:
                return {"ok": False, "stage": "build", **_trim(build_res, 120)}
        app = posixpath.join(d, "build/ios/iphonesimulator/Runner.app")
        mac.run(f"xcrun simctl install {udid} {shlex.quote(app)}", check=True, timeout=300)
        launch = mac.run(f"xcrun simctl launch --terminate-running-process {udid} {bundle}", timeout=120)
    return {"ok": launch["exit_code"] == 0, "simulator": udid, "bundle_id": bundle,
            "launch": launch["stdout"].strip() or launch["stderr"].strip(),
            "next": "mac_screenshot to see it, mac_simulator_logs for logs, mac_open_url for deep links."}


def install_app_zip(local_zip: str, device: Optional[str], bundle_id: Optional[str]) -> dict:
    data = Path(local_zip).expanduser().read_bytes()
    with Mac() as mac:
        udid = _pick_sim(mac, device)
        tmpdir = f"/tmp/fib-app-{int(time.time())}"
        mac.run(f"mkdir -p {tmpdir}", check=True)
        mac.upload_bytes(data, f"{tmpdir}/app.zip")
        mac.run(f"cd {tmpdir} && unzip -qo app.zip && xcrun simctl boot {udid} 2>/dev/null; "
                f"xcrun simctl bootstatus {udid} -b && xcrun simctl install {udid} {tmpdir}/*.app", check=True, timeout=300)
        launched = None
        if bundle_id:
            launched = mac.run(f"xcrun simctl launch {udid} {bundle_id}")["stdout"].strip()
    return {"installed_on": udid, "launched": launched}


def screenshot(device: Optional[str]) -> bytes:
    with Mac() as mac:
        udid = _pick_sim(mac, device)
        path = f"/tmp/fib-shot-{int(time.time()*1000)}.png"
        mac.run(f"xcrun simctl io {udid} screenshot --type=png {path}", check=True, timeout=60)
        data = mac.read_bytes(path)
        mac.run(f"rm -f {path}")
    return data


def simulator_logs(device: Optional[str], seconds: int, process: str, grep: Optional[str]) -> dict:
    with Mac() as mac:
        udid = _pick_sim(mac, device)
        pred = f'process == "{process}"'
        cmd = f"xcrun simctl spawn {udid} log show --style compact --last {int(seconds)}s --predicate {shlex.quote(pred)}"
        if grep:
            cmd += f" | grep -i -- {shlex.quote(grep)}"
        res = mac.run(cmd, timeout=120)
    return {"simulator": udid, "log": _tail(res["stdout"], 300), "stderr": _tail(res["stderr"], 20)}


def open_url(url: str, device: Optional[str]) -> dict:
    with Mac() as mac:
        udid = _pick_sim(mac, device)
        res = mac.run(f"xcrun simctl openurl {udid} {shlex.quote(url)}")
    return {"simulator": udid, "exit_code": res["exit_code"], "stderr": res["stderr"].strip()}


def fetch(remote_path: str, local_dest: str, project_path: Optional[str]) -> dict:
    with Mac() as mac:
        rp = remote_path
        if not rp.startswith(("/", "~")):
            rp = posixpath.join(mac.project_dir(resolve_project(project_path).name), rp)
        p = mac.download(rp, Path(local_dest).expanduser())
    return {"local_path": str(p), "bytes": p.stat().st_size}
