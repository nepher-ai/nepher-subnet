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

## 6. Root cause (confirmed 2026-03-31)

The `kind=f, size=0` error is caused by a **NumPy 2.x C ABI incompatibility** with Isaac Sim 5.1.0's compiled C++ extensions.

### Failure chain

1. **Torch reinstallation upgrades NumPy to 2.x.** The Dockerfile `pip install --force-reinstall torch` pulls in NumPy 2.2.6, replacing the 1.x version bundled with Isaac Sim 5.1.0.
2. **NumPy 2.0 broke the C ABI.** Isaac Sim's C++ plugins (`omni.syntheticdata.plugin`, warp, etc.) were compiled against the NumPy 1.x C ABI. NumPy 2.0 [changed type constants, array descriptor structs, and buffer protocol semantics](https://numpy.org/doc/stable/release/2.0.0-notes.html#numpy-2-0-migration-guide). Binary extensions must be recompiled for 2.x.
3. **Render variable registration corrupted.** When `omni.syntheticdata.plugin` calls NumPy C API functions (e.g. `PyArray_DescrFromType`) using 1.x constants, NumPy 2.x interprets them differently, producing a **corrupted dtype descriptor** (kind='f', itemsize=0). The `DistanceToCameraSD` render variable fails to materialize: `SdPostRenderVarTextureToBuffer missing valid input renderVar DistanceToCameraSD`.
4. **Warp crashes on the corrupted buffer.** Isaac Lab's `TiledCamera._update_buffers_impl()` calls `annotator.get_data()`, receives a numpy array with the degenerate dtype, and `wp.array()` raises `TypeError: Unable to write from unknown dtype, kind=f, size=0`.

### Evidence

- The error is **100% reproducible with or without GPU driver alignment**. Running with the original entrypoint (no shim, no LD_LIBRARY_PATH override) produces the identical error, proving the GPU driver was never the cause.
- Kit correctly detects the host driver (580.126.20) and Vulkan is functional — the RTX renderer initializes, but the syntheticdata pipeline fails at the NumPy C API boundary.
- `EXPORT_NONE` was already set, ruling out HDF5 recorder.
- `use_cache=False` ruled out terrain cache persistence.
- NumPy 2.2.6 was confirmed via `pip show numpy` inside the running container.

### Fix

Pin NumPy to 1.x in `Dockerfile.sandbox` **after** the torch reinstallation:

```dockerfile
RUN ${ISAACLAB_PATH}/isaaclab.sh -p -m pip install --no-cache-dir "numpy>=1.24,<2"
```

All downstream dependencies (`eval-nav`, `nepher`, `prettytable`, `hidapi`) require only `numpy>=1.20` and are satisfied by 1.24–1.26.

### Previous hypothesis (disproved): GPU driver library shadowing

The initial investigation (2026-03-30) hypothesised that bundled driver libraries in the Isaac Sim image shadowed host-mounted libs, preventing RTX from starting. While this *can* be a real issue on older hosts (e.g. driver 535.32 bundled vs 535.288 host), it was not the cause in the 580.x driver case. The `_align_gpu_driver_libs()` shim in `entrypoint-sandbox.sh` is retained as a safety net for future driver/GPU combinations but does not fix this particular error.

## 7. Previous fix (task-spot-nav)

`GoalNavStudentEnvCfg_Envhub_PLAY` sets `recorders.dataset_export_mode` to **`DatasetExportMode.EXPORT_NONE`**. This was a reasonable hypothesis but **did not address the root cause**. It remains a good defensive measure for environments where HDF5 recording is not needed.
