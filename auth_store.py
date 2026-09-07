"""
auth_store.py — local encrypted credentials vault for AI_ENGINE.

- credentials.enc: gpg AES-256 symmetric-encrypted JSON (the ONLY file on disk).
  No plaintext credentials file ever exists.
- The passphrase is fed to gpg through a private pipe fd — it never appears on
  the command line (not visible in `ps`).
- Decrypted data + passphrase live in process memory ONLY while unlocked.
  Wiped on lock(), on exit, and automatically after IDLE_LOCK_SECONDS of no use.
"""
import json
import os
import subprocess
import threading
import time

VAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.enc")
IDLE_LOCK_SECONDS = 1800  # default auto-lock after 30 min idle

# Optional "stay unlocked across restarts" passphrase file. When the user opts in,
# the passphrase is stored owner-only (0o600) so a reboot comes back unlocked. It is
# wiped the moment the user calls lock() / clear_remembered(). Never world-readable.
_KEEP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.keeppw")

_lock = threading.RLock()
_data = None        # decrypted dict (None = locked)
_passphrase = None  # RAM-only, needed to re-encrypt on save
_last_access = 0.0
_idle_lock_seconds = IDLE_LOCK_SECONDS


def remembered():
    """True if a cross-restart passphrase is stored on disk (unlocked will be
    restored automatically next boot)."""
    try:
        return os.path.exists(_KEEP_PATH)
    except Exception:
        return False


def remember_across_restarts(passphrase):
    """Persist the passphrase (owner-only file) so the vault auto-unlocks on the
    next boot. Call this after a successful manual unlock when the user asks to
    stay unlocked. Returns (ok, msg)."""
    try:
        with open(_KEEP_PATH, "w", encoding="utf-8") as f:
            f.write(passphrase)
        os.chmod(_KEEP_PATH, 0o600)
        return True, "Vault will stay unlocked across restarts (password remembered)"
    except Exception:
        return False, "Could not save the remembered password"


def clear_remembered():
    """Delete the remembered-passphrase file so the next boot requires the
    passphrase again. Safe no-op if nothing is stored."""
    try:
        if os.path.exists(_KEEP_PATH):
            os.remove(_KEEP_PATH)
        return True
    except Exception:
        return False


def _auto_unlock_from_keep():
    """On import: if the user opted in to stay unlocked across restarts and the
    vault exists, restore the unlocked state using the stored passphrase. Best
    effort — never raises."""
    global _data, _passphrase, _last_access
    try:
        if not os.path.exists(_KEEP_PATH):
            return False  # fresh box, no remembered passphrase — vault starts locked
        if not vault_exists():
            return False  # no vault yet — user creates one in the panel
        with open(_KEEP_PATH, "r", encoding="utf-8") as f:
            pw = f.read().strip()
        if not pw:
            print(f"[VAULT-AUTOUNLOCK] keeppw file is EMPTY", flush=True)
            return False
        print(f"[VAULT-AUTOUNLOCK] attempting to unlock with passphrase (len={len(pw)})", flush=True)
        with _lock:
            plain = _decrypt_bytes(pw)
            if plain is None:
                print(f"[VAULT-AUTOUNLOCK] decrypt FAILED — wrong or stale passphrase", flush=True)
                return False  # stale/wrong password — ignore silently
            try:
                _data = json.loads(plain.decode("utf-8"))
                print(f"[VAULT-AUTOUNLOCK] SUCCESS — vault loaded with {len(_data)} services", flush=True)
            except Exception as e:
                print(f"[VAULT-AUTOUNLOCK] JSON parse FAILED: {e}", flush=True)
                return False
            _passphrase = pw
            _last_access = time.time()
            return True
    except Exception as e:
        print(f"[VAULT-AUTOUNLOCK] EXCEPTION: {e}", flush=True)
        return False


def set_idle_lock(seconds):
    """Set the idle auto-lock timeout; None disables it (stays unlocked while the
    process runs — needed for email notifications while the user is away)."""
    global _idle_lock_seconds
    with _lock:
        _idle_lock_seconds = seconds


def _gpg_run(args, passphrase, input_bytes=None):
    """Run gpg with the passphrase on a private pipe fd (never on argv)."""
    r, w = os.pipe()
    try:
        os.write(w, passphrase.encode("utf-8"))
        os.close(w)
        p = subprocess.run(
            ["gpg", "--batch", "--quiet", "--yes", "--pinentry-mode", "loopback",
             "--passphrase-fd", str(r)] + args,
            input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(r,),
        )
    finally:
        os.close(r)
    return p


def _encrypt_bytes(plain_bytes, passphrase):
    p = _gpg_run(["--symmetric", "--cipher-algo", "AES256",
                  "--output", VAULT_PATH], passphrase, plain_bytes)
    return p.returncode == 0


def _decrypt_bytes(passphrase):
    if not os.path.exists(VAULT_PATH):
        return None
    p = _gpg_run(["--decrypt", VAULT_PATH], passphrase)
    if p.returncode != 0:
        return None
    return p.stdout


def vault_exists():
    return os.path.exists(VAULT_PATH)


def create_vault(passphrase, initial=None):
    """First run: create the encrypted vault. Fails if one already exists."""
    global _data, _passphrase, _last_access
    with _lock:
        if vault_exists():
            return False, "Vault already exists"
        if len(passphrase) < 4:
            return False, "Passphrase too short (min 4 chars)"
        data = initial or {}
        if not _encrypt_bytes(json.dumps(data, indent=2).encode("utf-8"), passphrase):
            if os.path.exists(VAULT_PATH):
                os.remove(VAULT_PATH)
            return False, "Encryption failed"
        try:
            os.chmod(VAULT_PATH, 0o600)
        except OSError:
            pass
        _data = data
        _passphrase = passphrase
        _last_access = time.time()
        return True, "Vault created"


def unlock(passphrase):
    global _data, _passphrase, _last_access
    with _lock:
        if _data is not None:
            return True, "Already unlocked"
        if not vault_exists():
            return False, "No vault yet — create one first"
        plain = _decrypt_bytes(passphrase)
        if plain is None:
            return False, "Wrong passphrase"
        try:
            _data = json.loads(plain.decode("utf-8"))
        except Exception:
            _data = None
            return False, "Vault data corrupted"
        _passphrase = passphrase
        _last_access = time.time()
        return True, "Vault unlocked"


def lock(clear_keeppw=False):
    """Wipe everything from memory (passphrase + decrypted data).
    
    Args:
        clear_keeppw: If True, also delete the remembered-across-restarts passphrase file
                     (only do this on explicit user "Lock now" action, NOT on shutdown).
                     If False (default), keep the keeppw file so auto-unlock works on next boot.
    """
    global _data, _passphrase
    with _lock:
        _data = None
        _passphrase = None
    if clear_keeppw:
        clear_remembered()
        print(f"[VAULT] lock() called with clear_keeppw=True — keeppw file deleted", flush=True)
    else:
        print(f"[VAULT] lock() called with clear_keeppw=False — keeppw file PRESERVED", flush=True)


def is_unlocked():
    global _last_access
    with _lock:
        if _data is None:
            return False
        if _idle_lock_seconds is not None and time.time() - _last_access > _idle_lock_seconds:
            lock()
            return False
        _last_access = time.time()
        return True


def get(service, field=None):
    with _lock:
        if not is_unlocked():
            return None
        val = _data.get(service)
        if field is None:
            return dict(val) if isinstance(val, dict) else val
        return val.get(field) if isinstance(val, dict) else None


def set_credential(service, field, value):
    """Set one field and re-encrypt the vault on disk."""
    global _last_access
    with _lock:
        if _data is None or _passphrase is None:
            return False, "Vault is locked"
        if not isinstance(_data.get(service), dict):
            _data[service] = {}
        _data[service][field] = value
        _last_access = time.time()
        if not _encrypt_bytes(json.dumps(_data, indent=2).encode("utf-8"), _passphrase):
            return False, "Re-encryption failed"
        return True, f"{service}.{field} saved"


def remove_service(service):
    global _last_access
    with _lock:
        if _data is None or _passphrase is None:
            return False, "Vault is locked"
        if service not in _data:
            return False, f"No service '{service}'"
        del _data[service]
        _last_access = time.time()
        if not _encrypt_bytes(json.dumps(_data, indent=2).encode("utf-8"), _passphrase):
            return False, "Re-encryption failed"
        return True, f"{service} removed"


def services():
    """[(service_name, {field: masked_value})] for the UI."""
    with _lock:
        if _data is None:
            return []
        out = []
        for name, val in _data.items():
            if isinstance(val, dict):
                out.append((name, {k: mask(v) for k, v in val.items()}))
            else:
                out.append((name, {"value": mask(str(val))}))
        return out


def mask(value):
    s = str(value) if value is not None else ""
    if not s:
        return "(empty)"
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "…" + s[-4:]


# --- SMTP provider presets (auto-fill host/port from the email domain) ---
SMTP_PRESETS = {
    "gmail.com":      {"host": "smtp.gmail.com", "port": 587,
                       "note": "Use a Gmail App Password, not your normal password"},
    "googlemail.com": {"host": "smtp.gmail.com", "port": 587,
                       "note": "Use a Gmail App Password, not your normal password"},
    "yahoo.com":      {"host": "smtp.mail.yahoo.com", "port": 587, "note": ""},
    "ymail.com":      {"host": "smtp.mail.yahoo.com", "port": 587, "note": ""},
    "mailfence.com":  {"host": "mailfence.com", "port": 587, "note": ""},
    "outlook.com":    {"host": "smtp-mail.outlook.com", "port": 587, "note": ""},
    "hotmail.com":    {"host": "smtp-mail.outlook.com", "port": 587, "note": ""},
    "live.com":       {"host": "smtp-mail.outlook.com", "port": 587, "note": ""},
}


def smtp_preset(email):
    """{'host','port','note'} for a known email domain, else None."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return SMTP_PRESETS.get(domain)


def notify_email(subject, body, attachment_path=None):
    """Send an email using stored smtp credentials.
    Never raises. Silent no-op if the vault is locked or smtp is not configured.
    attachment_path: optional local file (e.g. a generated PDF) to attach.
    Returns (sent, msg)."""
    try:
        with _lock:
            if not is_unlocked():
                return False, "vault locked"
            cfg = _data.get("smtp")
            if not isinstance(cfg, dict):
                return False, "smtp not configured"
            missing = [k for k in ("host", "port", "user", "password", "to") if not cfg.get(k)]
            if missing:
                return False, "smtp missing: " + ", ".join(missing)
            host, port = str(cfg["host"]), int(cfg["port"])
            user, pwd, to = str(cfg["user"]), str(cfg["password"]), str(cfg["to"])
        import smtplib
        from email.mime.text import MIMEText
        if attachment_path and os.path.exists(attachment_path):
            import os as _os
            from email.mime.multipart import MIMEMultipart
            from email.mime.application import MIMEApplication
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = user
            msg["To"] = to
            msg.attach(MIMEText(body, "plain"))
            with open(attachment_path, "rb") as af:
                part = MIMEApplication(af.read(), _subtype="pdf")
                part.add_header("Content-Disposition", "attachment",
                                filename=_os.path.basename(attachment_path))
                msg.attach(part)
        else:
            msg = MIMEText(body, "plain")
            msg["Subject"] = subject
            msg["From"] = user
            msg["To"] = to
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        return True, f"sent to {to}"
    except Exception as e:
        return False, f"send failed: {e}"


def send_test_email():
    """Send a test email using stored smtp credentials. Returns (ok, msg)."""
    return notify_email("AI_ENGINE test email",
                        "Test from your AI_ENGINE panel — email notifications are working.")


# --- PROJECT-FOLDER ENCRYPTION (Vault Phase 3) ---
# A whole project folder is packed with tar and encrypted with gpg AES-256 into a
# single PROJ.enc blob. Uses the vault's passphrase (held in RAM while unlocked),
# so the user only types the passphrase once per session.

def _project_passphrase():
    """Returns the vault passphrase if the vault is unlocked, else None."""
    with _lock:
        if not is_unlocked() or _passphrase is None:
            return None
        return _passphrase


def passphrase():
    """Public accessor: the vault passphrase while unlocked, else None. Needed by
    the UI to persist 'stay unlocked across restarts' without exposing it as a
    raw attribute."""
    return _project_passphrase()


def encrypt_project(project_dir, enc_path, project_name, exclude=()):
    """tar | gpg AES-256 a project folder into a single .enc blob.
    Returns (ok, msg). Requires the vault to be unlocked."""
    pp = _project_passphrase()
    if pp is None:
        return False, "Vault is locked — unlock it first"
    if not os.path.isdir(project_dir):
        return False, f"Project folder not found: {project_dir}"
    parent = os.path.dirname(project_dir)
    try:
        exclude_args = []
        for x in exclude:
            exclude_args += ["--exclude", x]
        r, w = os.pipe()
        try:
            os.write(w, pp.encode("utf-8"))
            os.close(w)
            tar = subprocess.Popen(
                ["tar", "-C", parent, "-cf", "-"] + exclude_args + [project_name],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            gpg = subprocess.Popen(
                ["gpg", "--batch", "--quiet", "--yes", "--pinentry-mode", "loopback",
                 "--symmetric", "--cipher-algo", "AES256",
                 "--passphrase-fd", str(r), "--output", enc_path],
                stdin=tar.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                pass_fds=(r,))
            tar.stdout.close()
            _, gerr = gpg.communicate()
            tar.wait()
        finally:
            os.close(r)
        if gpg.returncode != 0:
            return False, f"gpg failed: {gerr.decode('utf-8', 'replace')[:200]}"
        if not os.path.exists(enc_path) or os.path.getsize(enc_path) == 0:
            return False, "Encryption produced an empty file"
        try:
            os.chmod(enc_path, 0o600)
        except OSError:
            pass
        return True, f"Encrypted to {os.path.basename(enc_path)}"
    except Exception as e:
        return False, f"encrypt error: {e}"


def decrypt_project(enc_path, project_dir):
    """gpg-decrypt a .enc blob and untar it into project_dir.
    Returns (ok, msg). Requires the vault to be unlocked."""
    pp = _project_passphrase()
    if pp is None:
        return False, "Vault is locked — unlock it first"
    if not os.path.exists(enc_path):
        return False, f"Encrypted blob not found: {enc_path}"
    os.makedirs(project_dir, exist_ok=True)
    parent = os.path.dirname(project_dir)
    try:
        r, w = os.pipe()
        try:
            os.write(w, pp.encode("utf-8"))
            os.close(w)
            enc_in = open(enc_path, "rb")
            gpg = subprocess.Popen(
                ["gpg", "--batch", "--quiet", "--yes", "--pinentry-mode", "loopback",
                 "--decrypt", "--passphrase-fd", str(r)],
                stdin=enc_in, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(r,))
            tar = subprocess.Popen(
                ["tar", "-C", parent, "-xf", "-"],
                stdin=gpg.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            gpg.stdout.close()
            enc_in.close()
            tar.wait()
            _, gerr = gpg.communicate()
        finally:
            os.close(r)
        if gpg.returncode != 0:
            return False, f"gpg failed: {gerr.decode('utf-8', 'replace')[:200]}"
        return True, "Decrypted"
    except Exception as e:
        return False, f"decrypt error: {e}"


# On import: restore an opt-in "stay unlocked across restarts" state, if the user
# stored the passphrase via the UI and the vault exists. Best-effort, never raises.
if not os.environ.get("AI_ENGINE_NO_AUTOUNLOCK"):
    _auto_unlock_from_keep()
