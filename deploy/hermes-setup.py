from pathlib import Path
import os
import secrets
import json
import sys
import subprocess
import pwd
import shutil

sys.path.insert(0, "/data/workspace/darya")
from app.store import private_json, Store
from dotenv import set_key

root = Path("/data/workspace/darya")
data = root / "data"
home = Path("/data/workspace/alisa-hermes")
home.mkdir(parents=True, exist_ok=True, mode=0o700)
try:
    pwd.getpwnam("alisa-hermes")
except KeyError:
    subprocess.run(["sudo", "useradd", "--system", "--home-dir", str(home), "--shell", "/usr/sbin/nologin", "alisa-hermes"], check=True)
subprocess.run(["sudo", "usermod", "--home", str(home), "alisa-hermes"], check=True)
subprocess.run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(home)], check=True)
(home / "workspace").mkdir(exist_ok=True, mode=0o700)
provider_key = os.getenv("ALISA_AI_KEY", "")
if not provider_key:
    raise SystemExit("Provider Secret is not present in the shell environment")
secret_path = data / "secrets.json"
stored = json.loads(secret_path.read_text())
if not stored.get("ai_key_host"):
    stored.pop("ai_key", None)
api_key = stored.get("hermes_key") or secrets.token_urlsafe(40)
stored["hermes_key"] = api_key
private_json(secret_path, stored)
config = {
    "_config_version": 12,
    "model": {"default": "claude-haiku-4-5", "provider": "custom", "base_url": "https://api.xpiki.com/v1", "api_key": provider_key},
    "terminal": {"backend": "local", "cwd": str(home / "workspace")},
    "memory": {"memory_enabled": False, "user_profile_enabled": False, "nudge_interval": 0},
    "gateway": {"platforms": {"api_server": {"enabled": True, "host": "127.0.0.1", "port": 8642}}},
    "platform_toolsets": {"api_server": ["terminal", "file", "web", "browser", "skills"]},
    "agent": {"max_turns": 20},
}
# JSON is valid YAML. Keep the isolated Hermes profile independent of the user's home.
private_json(home / "config.yaml", config)
# Reuse the installed agent environment without running its source updater
# under a different Linux user. The gateway module is Hermes' own entry point.
engine = home / "engine"
if not engine.exists():
    previous = Path("/data/workspace/alisa/data/hermes/installs")
    candidates = subprocess.check_output(["sudo","find",str(previous),"-path","*/environments/*/venv","-type","d"],text=True).splitlines()
    if not candidates:
        raise SystemExit("Prepare a Hermes agent environment before installing the gateway service")
    subprocess.run(["sudo", "cp", "-a", str(candidates[0]), str(engine)], check=True)
interpreter = Path('/data/home/.hermes/tools/python-3.14.9+20261021-linux-x64')
if not (home/'python').exists():
    shutil.copytree(interpreter,home/'python',symlinks=True)
if not (home/'agent').exists():
    shutil.copytree('/data/home/.hermes/hermes-agent',home/'agent',symlinks=True,
                   ignore=shutil.ignore_patterns('.git','.hermes','__pycache__','.venv','.env'))
for link in (engine/'bin').iterdir():
    if link.name.startswith('python') and link.is_symlink():
        link.unlink()
        link.symlink_to(home/'python/bin/python3')
venv_config=engine/'pyvenv.cfg'
venv_config.write_text(venv_config.read_text().replace(str(interpreter),str(home/'python')))
ca_path = Path(os.getenv("SSL_CERT_FILE", "/run/instabox/secrets/ca-bundle.pem"))
shutil.copyfile(ca_path, home / "ca-bundle.pem")
env = home / ".env"
env.touch(mode=0o600, exist_ok=True)
env.chmod(0o600)
for key, value in {"API_SERVER_ENABLED": "true", "API_SERVER_HOST": "127.0.0.1", "API_SERVER_PORT": "8642", "API_SERVER_KEY": api_key,
                   "OPENAI_API_KEY": provider_key, "SSL_CERT_FILE": str(home / "ca-bundle.pem"),
                   "REQUESTS_CA_BUNDLE": str(home / "ca-bundle.pem")}.items():
    set_key(str(env), key, value)
(home / "SOUL.md").write_text("Ты Hermes, исполнитель задач Дарьи. Используй русский язык. Выполняй поручения в своей рабочей папке, сообщай реальные результаты. Telegram и подарки выполняются отдельными инструментами Дарьи.\n")
for key, value in {"ALISA_AI_KEY": provider_key, "ALISA_AI_KEY_HOST": "api.xpiki.com", "HERMES_API_URL": "http://127.0.0.1:8642"}.items():
    set_key(str(root / ".env"), key, value)
db = Store(data)
db.update_settings({"ai_base_url": "https://api.xpiki.com/v1", "ai_model": "claude-haiku-4-5"})
db.close()
subprocess.run(["sudo", "chown", "-R", "alisa-hermes:alisa-hermes", str(home)], check=True)
reader = pwd.getpwuid(os.getuid()).pw_name
subprocess.run(["sudo", "setfacl", "-m", f"u:{reader}:--x", str(home)], check=True)
subprocess.run(["sudo", "setfacl", "-m", f"u:{reader}:r-x,d:u:{reader}:r-x", str(home / "workspace")], check=True)
print("Hermes profile and scoped provider credentials configured")
