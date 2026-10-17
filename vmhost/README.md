# vmhost — VMware Workstation host monitor

Runs on the **Windows host** (not inside the lab) and shows, per running VM:

- host CPU used (percent of the host and number of cores), peak, RAM in use vs configured, disk I/O
- **per-vCPU load** — VMware names each vCPU thread `vcpu-N`, so a pegged firewall core shows up as one full bar
- busy VMware helper threads (SVGA, MKS, disk I/O)
- what the VM was given (.vmx: vCPUs, RAM, hardware version, guest OS, which VMnet each NIC is on)
- execution mode from `vmware.log` (`Monitor Mode: CPL0` = Native, `ULM` = Hyper-V/WHP) and recent warnings/errors

and for the host: per-logical-CPU load, memory, physical disks, every network adapter
(VMware's `VMnetX` adapters included) and the busiest non-VM processes. Every sample
is stored in `history.sqlite` (3 days by default, roughly 3 MB per VM per day) and drawn as a history chart.

If chkp-monitor is running inside the lab, vmhost also pulls its `/api/status` and shows
the Check Point view (CPU per core, CoreXL instances, connections per virtual system)
on the matching VM card — matched by name, so the VM display name must equal the
gateway name in chkp-monitor's config (e.g. `A-VSX-01`).

## Run

```powershell
cd <repo>\vmhost
powershell -ExecutionPolicy Bypass -File .\run.ps1     # from an elevated PowerShell
```

`run.ps1` creates a `.venv`, installs `psutil`, opens http://127.0.0.1:8090 and starts the
monitor. Python 3.10+ is required (`winget install --id Python.Python.3.12 --scope user`).
Everything stays in this folder: delete it to remove the monitor.

**Run it elevated.** Without elevation Windows hides the vmware-vmx threads, so per-vCPU
load is missing. VM names still resolve (from `vmrun list` + `vmware.log`), just a
few seconds later.

## Configure (`config.json`)

| Key | Meaning |
|---|---|
| `vm_roots` | Folders searched for running VMs' `vmware.log` (default `E:\VirtualMachines`) |
| `sample_seconds` | Sample interval (default 2) |
| `lab_status_url` | chkp-monitor JSON inside the lab, e.g. `http://192.168.50.10:8080/api/status`; `null` to disable |
| `listen` / `port` | Dashboard address; keep `127.0.0.1` so only this PC can open it |
| `history_days` | How long samples are kept |

## Reading it

- **CPU (host)** is a share of all logical processors: one busy vCPU on a 32-thread host is about 3%. The **cores** figure underneath is easier to read: 1.00 = one full core.
- A vCPU bar near 100% while the guest reports low CPU usually means the guest is spinning or waiting on the host. If it matches the guest's per-core view, the guest is genuinely busy.
- **VMnet rows** only count traffic between the host and that virtual network. VM-to-VM traffic on the same VMnet, and LAN segments, never pass through a host adapter.
