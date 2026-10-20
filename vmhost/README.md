# VM Host Monitor — install and user guide

A local web dashboard for a Windows PC running **VMware Workstation**. It shows what each
running VM is costing the host, down to each virtual CPU, so you can tell which VM (and
which vCPU inside it) is using your CPU, memory and disk.

It only **reads** information. It never changes VMs or VMware settings, and the dashboard
is only reachable from the PC itself (`http://127.0.0.1:8090`).

---

## 1. What you need

- Windows 10/11 with VMware Workstation, and the VMs you want to watch
- Internet access for the first install (Python and one Python package)
- An account that can **Run as administrator** (needed for per-vCPU load)

Everything installs into one folder plus a per-user Python. Disk use is small; history is
about 3 MB per VM per day, kept for 3 days.

---

## 2. Install on a new computer

### Step 1 — Install Python (once per computer)

Open PowerShell (normal, not admin) and run:

```powershell
winget install --id Python.Python.3.12 --scope user --source winget
```

Close PowerShell, open a new window, and check:

```powershell
py --version        # should print Python 3.12.x
```

> If `winget` is missing, install Python from https://www.python.org/downloads/ instead
> (tick "Add python.exe to PATH").

### Step 2 — Download the tool

```powershell
New-Item -ItemType Directory C:\Tools -Force | Out-Null
Invoke-WebRequest https://github.com/Don-Paterson/chkp-monitor/archive/refs/heads/vsx-lab-and-vmhost.zip -OutFile $env:TEMP\chkp-monitor.zip
Expand-Archive $env:TEMP\chkp-monitor.zip C:\Tools -Force
Rename-Item C:\Tools\chkp-monitor-vsx-lab-and-vmhost chkp-monitor
Get-ChildItem C:\Tools\chkp-monitor -Recurse | Unblock-File
```

> **Branch name:** the tool currently lives on the `vsx-lab-and-vmhost` branch. Once that is
> merged into `main`, replace `vsx-lab-and-vmhost` with `main` in the two places above
> (the URL, and the folder name `chkp-monitor-main`).
>
> If the download returns **404**, the repository is private: open the URL in a browser
> while logged in to GitHub, save the zip, and run the last three lines against it.
>
> With Git installed you can instead run:
> `git clone -b vsx-lab-and-vmhost https://github.com/Don-Paterson/chkp-monitor C:\Tools\chkp-monitor`

### Step 3 — Tell it where your VMs live

```powershell
notepad C:\Tools\chkp-monitor\vmhost\config.json
```

Set `vm_roots` to the folder(s) holding your VMs — backslashes doubled:

```json
"vm_roots": ["E:\\VirtualMachines\\CheckPoint\\CCVS-R8220"],
```

This is only used to put names on VMs; every running VM is shown regardless.

### Step 4 — First start (as Administrator)

Start → type **Terminal** → right-click → **Run as administrator**
(the title bar must start with "Administrator:"), then:

```powershell
cd C:\Tools\chkp-monitor\vmhost
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

The first start takes about a minute (it creates `.venv` and installs `psutil`). Then your
browser opens **http://127.0.0.1:8090**. Check that each running VM shows its name, vCPU
bars and a **Native** badge.

---

## 3. Everyday use — the desktop app

### Create the shortcut (once)

In a normal PowerShell:

```powershell
cd C:\Tools\chkp-monitor\vmhost
powershell -ExecutionPolicy Bypass -File .\install-shortcut.ps1
```

This puts a **VM Host Monitor** icon on the desktop and in the Start menu. To pin it to the
taskbar, open Start, right-click **VM Host Monitor**, and choose **Pin to taskbar**. Run the
script again if you ever move the folder; add `-Remove` to delete the shortcuts.

### Use it

- **Start:** double-click **VM Host Monitor** and answer **Yes** to the Windows (UAC) prompt.
  The dashboard opens in its own window, with no browser tabs or address bar. On the very
  first start a setup window appears for about a minute; that happens only once.
- **Stop:** close the window. The monitor stops with it.
- **Clicked No at the UAC prompt?** It still runs, just without the per-vCPU bars.
- **Started it twice?** You just get a second window onto the same monitor.

The window uses Microsoft Edge (or Chrome) in "app mode", with its own profile, so it
remembers its size and position and does not touch your normal browser. Problems
starting are shown in a message box; details are in `vmhost\vmhost.log`.

### Without the shortcut (terminal)

In an **Administrator** terminal (Ctrl+C stops it):

```powershell
cd C:\Tools\chkp-monitor\vmhost
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

Then open http://127.0.0.1:8090 in any browser.

---

## 4. Reading the dashboard

### Header

| Item | Meaning |
|---|---|
| **CPU** | Whole-host CPU use, like Task Manager |
| **RAM** | Host memory in use. Above ~90% Windows starts paging and every VM slows down |
| **VMs** | Running VMware VMs |
| Blue ⓘ notes | Things limiting what can be shown (e.g. "not elevated") |

### Top panels

- **Logical processors** — one bar per host CPU thread (hover for the value).
- **Network adapters** — host adapters, including VMware's `VMnetX`. A VMnet row only counts
  traffic between the host and that virtual network; VM-to-VM traffic never appears here.
- **Physical disks** — read/write per disk. Sustained reads on the system disk while RAM is
  over 90% usually means paging.

### VM cards (busiest first)

| Item | Meaning |
|---|---|
| Title line | VM name, PID, configured vCPUs and RAM, hardware version, guest OS, and the VMnet of each NIC |
| **Native / WHP / Hyper-V** badge | How VMware runs the VM. *Native* is normal. *WHP/Hyper-V* means Windows Hyper-V/VBS is on: slower, and host CPU figures under-report. *mode ?* means the log had no mode line |
| **CPU (host)** | Share of the whole host. Easier: **cores** underneath — 1.00 = one full host core. *Peak* = highest since the monitor started |
| **RAM in use** | Host memory the VM actually occupies, vs. what it was configured with |
| **Disk read / write** | VM's disk I/O right now |
| **vCPU bars** | Load on each virtual CPU (100% = one full host core). **Red = 90% or more** |
| Busy VMware threads | VMware's own helper threads using >2% (graphics, disk I/O…) |
| Sparkline | VM CPU over the last 10 minutes |
| *N recent warnings/errors* | Click to read the last lines VMware logged as warnings. A handful is normal |

### History

Choose **host** or a VM, the metric (CPU, memory, disk I/O) and the range (10 min to 7 days).
Hover the chart for exact values. Gaps mean the monitor was not running.

### Other host processes

The busiest non-VMware programs — confirms whether something other than VMware is using the PC.

---

## 5. What common patterns mean

| You see | Likely meaning | What to do |
|---|---|---|
| One vCPU red (~100%), the others moderate — e.g. vCPU 0 on a Check Point gateway | One core does a job the others can't share (on Check Point, CPU 0 usually handles interrupts/SND and management daemons) | Inside the guest: `top` (press 1), `cpview` → CPU, `fw ctl affinity -l -r` |
| All vCPUs high, *cores* ≈ number of vCPUs | VM is using everything it was given | Check inside the guest; give it more vCPUs only if the host has spare cores |
| Host shows a vCPU at 100% but the guest's own `top` shows it idle | Guest is busy-waiting (polling) | A guest/VMware tuning issue rather than real load |
| RAM > 90% plus steady reads on the system disk | Host is paging | Stop VMs you don't need, or lower VM memory sizes |
| **WHP / Hyper-V** badge | Hyper-V, VBS or Memory Integrity is on in Windows | Optional: turn them off for native VMware speed |
| CPU high just after starting VMs | Boot work | Wait 5–10 minutes before judging |

---

## 6. Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `vm_roots` | `["E:\\VirtualMachines"]` | Folders searched (with subfolders) to name running VMs |
| `sample_seconds` | 2 | How often to sample |
| `port` | 8090 | Dashboard port; change if something else uses it |
| `listen` | `127.0.0.1` | Keep this so only this PC can open the dashboard |
| `history_days` | 3 | How long history is kept (`history.sqlite`) |
| `top_other` | 8 | Rows in "Other host processes" |
| `lab_status_url` | `null` | chkp-monitor JSON inside a lab, e.g. `http://<A-GUI>:8080/api/status`; adds the Check Point view to matching VM cards |
| `vmrun_path` | `null` | Only if VMware is installed somewhere unusual |

Restart the monitor after editing.

---

## 7. Updating to a newer version

1. Stop the monitor.
2. Copy `C:\Tools\chkp-monitor\vmhost\config.json` somewhere safe (and `history.sqlite` if you want to keep history).
3. Delete `C:\Tools\chkp-monitor` and repeat **Step 2** of the install.
4. Put your `config.json` back and start as usual (the first start rebuilds `.venv`).

With Git: `cd C:\Tools\chkp-monitor; git pull` — your `config.json` changes are kept unless the file changed upstream.

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| "Python 3.10 or newer was not found" | Do install Step 1, then open a **new** terminal |
| Note "Per-vCPU load needs the monitor to run elevated" | The terminal was not *Run as administrator*. Stop and restart elevated |
| A VM shows as `vmware-vmx (12345)` instead of its name | Add its parent folder to `vm_roots`, or run elevated; names can take ~15 seconds |
| "monitor not responding" in the browser | The monitor was stopped or crashed — check its terminal (or `vmhost.log` when started from the shortcut), then start it again |
| Shortcut does nothing, or shows "did not start within 30 seconds" | Read `C:\Tools\chkp-monitor\vmhost\vmhost.log`; try `run.ps1` in a terminal to see errors directly |
| Shortcut icon is blank after moving the folder | Run `install-shortcut.ps1` again |
| Browser doesn't open / page can't load | Wait a few seconds and refresh; if port 8090 is taken, change `port` |
| "running scripts is disabled on this system" | Use the full command with `-ExecutionPolicy Bypass`, and run `Unblock-File` (install Step 2) |
| `pip install psutil failed` | Check internet/proxy, then run `run.ps1` again — it retries |
| Something else broken | Delete `C:\Tools\chkp-monitor\vmhost\.venv` and start again |

---

## 9. Uninstall

1. Stop the monitor.
2. Remove the shortcuts: `powershell -ExecutionPolicy Bypass -File C:\Tools\chkp-monitor\vmhost\install-shortcut.ps1 -Remove`
3. Delete `C:\Tools\chkp-monitor` and `%LOCALAPPDATA%\VMHostMonitor` (the app window's profile).
4. Optional: `winget uninstall Python.Python.3.12`

Nothing else is changed on the PC.

---

## How it works (for reference)

- `install-shortcut.ps1` creates the shortcuts; they run `app.ps1`, which runs first-time setup (`run.ps1 -SetupOnly`), starts `vmhost.py` hidden and elevated with `--exit-with <launcher PID>`, and opens the Edge/Chrome app window. Closing the window ends the launcher, and the monitor then stops itself.
- `run.ps1` finds Python, creates `.venv`, installs `psutil`, and starts `vmhost.py` in the terminal.
- `vmhost.py` samples every running `vmware-vmx.exe`: CPU time, memory, I/O and per-thread
  CPU. VMware names each vCPU thread `vcpu-N`, which gives the per-vCPU bars. Windows only
  lets an administrator read those threads.
- VM names come from the process command line, or from `vmrun list` plus each VM's
  `vmware.log`, whose first line records the process ID. The `.vmx` file supplies the
  configuration, and `vmware.log` supplies the mode (`Monitor Mode: CPL0` = Native,
  `ULM` = Hyper-V/WHP) and warnings.
- Samples go into `history.sqlite`; the page (`static/index.html`) refreshes every 2 seconds.
