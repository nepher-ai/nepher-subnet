# Investigation: `Unable to write from unknown dtype, kind=f, size=0`

This document records findings for the `ENV_ERROR` / `TypeError` seen when loading `Nepher-Spot-Nav-Envhub-Student-Play-v0` with `env_id=obstacle-terrain-sample`.

## 1. Full traceback (how to capture)

`results.json` only wraps the exception message. To see whether the failure is in **NumPy**, **h5py**, **Isaac Lab terrains**, or **HDF5DatasetFileHandler**:

1. Use the sandbox log bundle: `entrypoint-sandbox.sh` copies the latest `eval_run_*` directory to `/sandbox/output/eval_logs` when present.
2. On the **host**, that volume is the validator’s `output` mount (see validator logs: `output=.../workspace/sandbox/<run_id>/output`). Inspect `.../output/eval_logs/eval_run_*/` for `results.json`, `summary.txt`, and any stderr capture; search for `Traceback (most recent call last)`.
3. The line **“There was an error running python”** comes from the Isaac Lab wrapper when the eval script exits non-zero—it is not the root cause by itself.

**Linux / Docker** (bash), correct stderr discard:

```bash
python -c "import numpy; import os, subprocess; p=os.path.dirname(numpy.__file__); r=subprocess.run(['grep','-R','Unable to write from unknown dtype',p],capture_output=True,text=True); print(r.stdout or r.stderr)" 2>/dev/null
```

Do **not** use `2>nul` on Linux; that is **cmd.exe** syntax and can confuse bash or create a file named `nul`.

## 2. Terrain cache vs HDF5 recorder

| Hypothesis | Why it fits |
|------------|----------------|
| **Terrain cache (`use_cache=True`)** | `obstacle_terrains.py` sets `cache_dir` under the env bundle. Isaac Lab’s generator persists arrays (often via NumPy `.npy`/binary paths). A **degenerate float dtype** or **unsupported array** during write triggers NumPy’s “unable to write” style errors. |
| **HDF5 dataset recorder** | Default `ManagerBasedRLEnvCfg` in Isaac Lab can include `RecorderManagerBaseCfg` + `HDF5DatasetFileHandler`. **h5py** maps NumPy dtypes to HDF5; **zero-sized float** or **odd dtypes** fail at dataset creation or first write. |

**Disambiguation:** The innermost frame in a full traceback names the module (`numpy`, `h5py`, `isaaclab.terrains`, `isaaclab.utils.datasets`, etc.).

### Empirical narrowing (2026-03-30 validator run)

A failing sandbox log showed the preset’s terrain generator with **`use_cache=False`** (while still listing `cache_dir`—the field is present even when caching is off). The same **`TypeError`** / `ENV_ERROR` still occurred, so **terrain cache file write is unlikely to be the cause** for that run. Prefer investigating **HDF5 recording**, **evaluator NumPy state dumps** (see `*.npy` under the eval log dir mentioned in stdout), **Isaac Lab env init** paths that still call NumPy binary I/O, or **Omniverse/physics** code that touches NumPy arrays with edge-case dtypes.

## 3. Version matrix (validator vs sandbox)

| Component | Sandbox image ([`docker/Dockerfile.sandbox`](../docker/Dockerfile.sandbox)) |
|-----------|--------------------------------------------------------------------------------|
| Isaac Sim | `nvcr.io/nvidia/isaac-sim:5.1.0` |
| Isaac Lab | `v2.3.2` (git clone in Dockerfile) |
| NumPy | **2.2.6** (confirmed via `pip show numpy` inside running container) |
| eval-nav | Installed editable from `EVAL_REPO_URL` (default public repo) |

The **validator** image ([`docker/Dockerfile.validator`](../docker/Dockerfile.validator)) is Python 3.10 + orchestration only; it does **not** run Isaac Sim. Version skew matters between **your local dev env** and the **sandbox** if you cannot reproduce locally.

## 4. Meaning of `kind=f, size=0`

- **`kind=f`**: NumPy dtype kind for **floating-point** (`float16`, `float32`, `float64`, …).
- **`size=0`**: In this message, **itemsize** of the dtype is **zero** (or the writer treats the float dtype as unsupported), which is invalid for normal floats and often appears when **serializing** (`.tofile`, `np.save`, HDF5) hits an edge case or **NumPy 2.x** stricter dtype paths.

So the failure is almost certainly **array persistence** during env construction, not JSON loading of `positions.json`.

## 5. Recommended next steps

1. Capture and archive the **full traceback** from `eval_logs` for the failing run.
2. If **`use_cache=False`** still fails (as in the 2026-03-30 run), **do not** focus on terrain cache pre-bake; inspect **`h5py` / `HDF5DatasetFileHandler`**, **evaluator `*.npy` artifacts**, and other init-time NumPy writers.
3. If the frame is `h5py` / `HDF5DatasetFileHandler`, disable or narrow dataset export in the play env cfg for evaluation builds.
4. Align **NumPy / h5py** with the versions Isaac Lab 2.3.2 tests against if a library mismatch is suspected.

## 6. Root cause (confirmed 2026-03-30)

The `kind=f, size=0` error is caused by an **RTX renderer failure** inside the sandbox container, not by HDF5/recorder or terrain cache writes.

### Failure chain

1. **Bundled driver shadows host driver.** The Isaac Sim base image (`nvcr.io/nvidia/isaac-sim:5.1.0`) ships Vulkan/GL userspace libraries for driver **535.32**. The NVIDIA Container Toolkit mounts the host's driver libraries (e.g. **535.288**), but the bundled copies appear earlier on `LD_LIBRARY_PATH` and shadow them.
2. **RTX renderer refuses to start.** Omniverse Kit's `gpu.foundation.plugin` sees driver 535.32 (below the 535.129 minimum for RTX) and rejects it: `rtx driver verification failed`.
3. **Depth camera produces a degenerate buffer.** The `TiledCamera` sensor (`data_types=["distance_to_camera"]`) requires the RTX renderer. Without it, the camera data tensor has a zero-sized float dtype.
4. **NumPy blows up.** Isaac Lab env initialisation tries to write/allocate from this buffer → `TypeError: Unable to write from unknown dtype, kind=f, size=0`.

### Evidence

- `nvidia-smi` inside the container reported the host version (535.288) because it always uses the host-mounted binary.
- The Omniverse Kit startup table reported `Driver Version: 535.32.01` — the bundled version.
- `EXPORT_NONE` was already set in the config dump, ruling out the HDF5 recorder.
- `use_cache=False` ruled out terrain cache persistence.

### Why simple `LD_LIBRARY_PATH` prepend is not enough

Isaac Sim's `setup_python_env.sh` (sourced by `python.sh` on every launch) **re-sets** `LD_LIBRARY_PATH` with its own directories first. Any prepend done in the entrypoint gets pushed to the back, and the bundled libraries in Isaac Sim's directories win again.

### Fix (v2 — three-layer override)

`entrypoint-sandbox.sh` now includes `_align_gpu_driver_libs()` with three layers:

1. **Override directory** (`/app/nvidia-driver-override/`): contains symlinks whose filenames match **both** the host version (`*.so.535.288.01`) **and** the bundled version (`*.so.535.32.01`) — all pointing to the host library. No matter which version the dynamic linker requests, it gets the host copy.
2. **Patch `setup_python_env.sh`**: appends a line at the END of Isaac Sim's env setup script that prepends the override directory to `LD_LIBRARY_PATH`. Because it runs last, it takes effect after Isaac Sim's own path setup.
3. **Vulkan ICD manifests**: updates all `nvidia_icd*.json` files (system-wide and inside the Isaac Sim tree) to reference the host version of `libnvidia-vulkan-producer.so`.

This runs automatically before the evaluation — no host-side changes required (though updating the NVIDIA Container Toolkit is still recommended).

## 7. Previous fix (task-spot-nav)

`GoalNavStudentEnvCfg_Envhub_PLAY` sets `recorders.dataset_export_mode` to **`DatasetExportMode.EXPORT_NONE`**. This was a reasonable hypothesis but **did not address the root cause**. It remains a good defensive measure for environments where HDF5 recording is not needed.
