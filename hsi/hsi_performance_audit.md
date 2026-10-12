# LAMDA HSI Pipeline — Comprehensive Performance & Architecture Audit

## High-Level Summary of Main Bottlenecks

| Priority | Area | Impact |
|----------|------|--------|
| **CRITICAL** | Sequential per-band I/O in `saveEnvi()` | Re-reads every band from disk (TIFF → float → ENVI), O(N²) I/O for N bands |
| **CRITICAL** | Scalar pixel loops in calibration & preview | `calib_->apply()`: 3 passes over full frame; `makePreviewBand()`: O(scale²) box filter |
| **HIGH** | No SIMD/vectorization anywhere | All per-pixel loops are scalar C++ — 4-8× slower than AVX2/NEON |
| **HIGH** | Blocking monochromator + camera serializes acquisition | ~1-2s dwell + move + exposure per band; no overlap |
| **HIGH** | Full-frame copies for display (preview + histogram) | `uploadFrameToTexture()` allocates + copies every frame |
| **MEDIUM** | TIFF writer allocates intermediate buffers | `ByteStream` + `std::vector` copies per band |
| **MEDIUM** | ENVI export loads all bands into memory | `saveEnvi()` reads all bands sequentially but could stream |

---

## Ranked Optimization Opportunities

### 1. ELIMINATE RE-READ IN `saveEnvi()` — CRITICAL

**Location:** `cube.cpp:294-400`

**Problem:** `saveEnvi()` re-reads every band from disk (TIFF → `readFloatTiff` → write to ENVI `.dat`). For 80 bands × 2048×2048 = ~1.3GB, this is **O(N_bands × frame_size) redundant I/O**.

**Current flow:**
```
ScanThread.run() → write TIFF (display) + write FloatTIFF (cache)
     ↓
saveEnvi() → read FloatTIFF cache → write ENVI .dat
```

**Fix:** Stream directly to ENVI during acquisition. Keep FloatTIFF cache only if user needs per-band TIFFs.

```cpp
// In ScanThread::run(), open ENVI .dat ONCE before loop:
std::ofstream enviDat(datPath, std::ios::binary);
std::vector<double> wavelengths;

// Inside wavelength loop (after calibration):
if (writeFloat) {
    enviDat.write(reinterpret_cast<const char*>(band.data.data()),
                  band.data.size() * sizeof(float));
} else {
    std::vector<uint16_t> u16(band.data.size());
    std::transform(band.data.begin(), band.data.end(), u16.begin(),
                   [](float v) { return std::clamp(v, 0.0f, 65535.0f); });
    enviDat.write(reinterpret_cast<const char*>(u16.data()),
                  u16.size() * sizeof(uint16_t));
}
wavelengths.push_back(wl);

// After loop: write .hdr only
```

**Impact:** Eliminates 100% of re-read I/O. 10-50× faster ENVI export.

---

### 2. VECTORIZE CALIBRATION APPLY — CRITICAL

**Location:** `calibration.cpp:265-305` (`CalibrationManager::apply`)

**Problem:** Three separate scalar loops over full frame (2048×2048 = 4.2M pixels):
1. Lift raw → float
2. Dark subtraction OR dark+flat correction
3. (Optional) `applyU16` does another loop

**Fix:** Single fused pass with SIMD. Use `#pragma omp simd` or explicit intrinsics.

```cpp
// FUSED + SIMD
const size_t n = raw.width * raw.height;
out.data.resize(n);
if (dark && flat) {
    double sum = 0;
    #pragma omp simd reduction(+:sum)
    for (size_t i = 0; i < n; ++i) sum += static_cast<double>(flat->data[i]) - dark->data[i];
    double meanFMD = sum / n;
    #pragma omp simd
    for (size_t i = 0; i < n; ++i) {
        float fmd = flat->data[i] - dark->data[i];
        fmd = fmd <= 1e-6f ? 1e-6f : fmd;
        out.data[i] = (rawf[i] - dark->data[i]) / fmd * static_cast<float>(meanFMD);
    }
} else if (dark) {
    #pragma omp simd
    for (size_t i = 0; i < n; ++i) out.data[i] = rawf[i] - dark->data[i];
} else {
    #pragma omp simd
    for (size_t i = 0; i < n; ++i) out.data[i] = rawf[i];
}
```

**Add to CMakeLists.txt:**
```cmake
target_compile_options(lamda_hsi PRIVATE $<$<CXX_COMPILER_ID:GNU,Clang>:-O3 -march=native -ftree-vectorize>)
```

**Impact:** 3-5× speedup on calibration (memory-bound → compute-bound with SIMD).

---

### 3. OPTIMIZE `makePreviewBand()` BOX FILTER — HIGH

**Location:** `cube.cpp:97-139`

**Problem:** Naive O(scale²) box filter. For 2048² → 512² (scale=4): 16 inner iterations per output pixel. No SIMD, cache-unfriendly strided access.

**Fix:** Two-pass separable box filter (O(2×scale) vs O(scale²)) + SIMD.

```cpp
CubeBand makePreviewBand(const CubeBand& full) {
    // ... compute scale, preview dims ...

    // Pass 1: horizontal box sum (SIMD-friendly)
    std::vector<float> hsum(preview.width * full.height);
    #pragma omp parallel for
    for (int y = 0; y < full.height; ++y) {
        float* rowOut = &hsum[y * preview.width];
        const float* rowIn = &full.data[y * full.width];
        for (int px = 0; px < preview.width; ++px) {
            int x0 = px * scale;
            int x1 = std::min(full.width, x0 + scale);
            float sum = 0;
            for (int x = x0; x < x1; ++x) sum += rowIn[x];
            rowOut[px] = sum / (x1 - x0);
        }
    }

    // Pass 2: vertical box average into final preview
    #pragma omp parallel for
    for (int py = 0; py < preview.height; ++py) {
        int y0 = py * scale;
        int y1 = std::min(full.height, y0 + scale);
        float* outRow = &preview.data[py * preview.width];
        for (int px = 0; px < preview.width; ++px) {
            float sum = 0;
            for (int y = y0; y < y1; ++y) sum += hsum[y * preview.width + px];
            outRow[px] = sum / (y1 - y0);
        }
    }
    return preview;
}
```

**Impact:** 4-8× faster preview generation. Critical for interactive cube browsing.

---

### 4. OVERLAP MONOCHROMATOR MOVE + CAMERA EXPOSURE — HIGH

**Location:** `cube.cpp:554-687` (`ScanThread::run`)

**Problem:** Strictly sequential:
```
for each wavelength:
    mono.goTo(wl)      // 0.8-2s mechanical move
    sleep(dwell)       // 0.2s settle
    camera.expose()    // exposure time
    camera.read()      // transfer
    calibrate + save
```

**Fix:** Pipeline stages — start next monochromator move while camera exposes/reads current band.

```cpp
// Pseudocode for pipelined acquisition:
struct BandJob { double wl; Frame raw; CubeBand calibrated; };

std::queue<BandJob> pipeline;
std::mutex pipeMtx;
std::condition_variable pipeCv;
bool done = false;

// Thread 1: Monochromator mover
auto mover = [&]() {
    for (double wl : wavelengths) {
        mono.goTo(wl);
        if (params.dwellSec > 0) sleep(dwell);
        {
            std::lock_guard lock(pipeMtx);
            pipeline.emplace(BandJob{wl, {}, {}});
        }
        pipeCv.notify_one();
    }
    done = true;
    pipeCv.notify_all();
};

// Thread 2: Camera + calibration + save
auto worker = [&]() {
    while (true) {
        BandJob job;
        {
            std::unique_lock lock(pipeMtx);
            pipeCv.wait(lock, [&]{ return !pipeline.empty() || done; });
            if (pipeline.empty() && done) break;
            job = std::move(pipeline.front());
            pipeline.pop();
        }
        camera.startExposure(false);
        wait_for_exposure();
        camera.getDataAfterExp(job.raw.data);
        job.calibrated = calibrate(job.raw);
        save_band(job);
    }
};
```

**Impact:** Overlaps ~1-2s monochromator move with ~0.1-10s exposure. **20-50% total scan time reduction** for typical dwell/exposure ratios.

---

### 5. ELIMINATE DISPLAY FRAME COPIES IN `uploadFrameToTexture()` — HIGH

**Location:** `gui.cpp:342-448`

**Problem:** Every preview frame:
1. Allocates `corrected16` vector (if calibration preview on)
2. Allocates `g8` output vector
3. Scalar loops for stretch + downscale
4. `glTexSubImage2D` upload

**Fix:** Reuse persistent buffers (pool), fuse stretch + downscale + calibration in one pass, use PBO (Pixel Buffer Object) for async GPU upload.

```cpp
// Persistent buffers (member variables)
std::vector<uint16_t> calibratedBuf_;
std::vector<uint8_t> displayBuf_;
GLuint pbo_ = 0;

// In uploadFrameToTexture:
if (calibratedBuf_.size() < n) calibratedBuf_.resize(n);
if (displayBuf_.size() < displaySize) displayBuf_.resize(displaySize);

if (applyCalibPreview_ && calib_ && (calib_->hasDark() || calib_->hasFlat())) {
    calib_->applyU16To(f, calibratedBuf_.data()); // in-place, no alloc
    stretch_downscale_u16_to_u8(calibratedBuf_.data(), displayBuf_.data(), ...);
} else {
    stretch_downscale_raw_to_u8(f.data.data(), displayBuf_.data(), ...);
}

// Async PBO upload
if (!pbo_) glGenBuffers(1, &pbo_);
glBindBuffer(GL_PIXEL_UNPACK_BUFFER, pbo_);
glBufferData(GL_PIXEL_UNPACK_BUFFER, displayBuf_.size(), displayBuf_.data(), GL_STREAM_DRAW);
glBindTexture(GL_TEXTURE_2D, previewTex_);
glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, outW, outH, GL_RED, GL_UNSIGNED_BYTE, 0);
glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
```

**Impact:** Removes per-frame allocations. PBO enables DMA transfer while CPU prepares next frame.

---

### 6. HISTOGRAM COMPUTATION ON FULL RES — MEDIUM

**Location:** `gui.cpp:450-529` (`computeHistogram`)

**Problem:** Computes histogram on full-resolution frame (4.2M pixels) every 4th preview frame. Uses scalar loop with branch-heavy binning.

**Fix:** Compute on downsampled preview (already available) or use incremental histogram computed during stretch_downscale pass.

---

### 7. TIFF WRITER: ELIMINATE `ByteStream` COPIES — MEDIUM

**Location:** `image_writer.cpp:114-221` (`writeTiff`, `writeGrey16Tiff`, `writeFloatTiff`)

**Problem:** `ByteStream` accumulates entire file in `std::vector<uint8_t>` → single `fwrite`. For 2048² × 16-bit = 8MB/band, this doubles memory and copies.

**Fix:** Write IFD + description directly to file, then stream pixel data in chunks.

```cpp
void writeGrey16Tiff(const string& path, int W, int H, const uint16_t* pixels, const string& desc) {
    ofstream f(path, ios::binary);
    // Write header + IFD directly to stream
    write_header_and_ifd(f, W, H, desc);
    // Stream pixel data in 1MB chunks
    const size_t chunk = 1024*1024;
    for (size_t offset = 0; offset < W*H; offset += chunk/2) {
        size_t n = min(chunk/2, (size_t)W*H - offset);
        f.write(reinterpret_cast<const char*>(pixels + offset), n * sizeof(uint16_t));
    }
}
```

**Impact:** Removes 8MB allocation+copy per band. Critical for high-band-count scans.

---

### 8. ENVI EXPORT: STREAM DON'T BUFFER — MEDIUM

**Location:** `cube.cpp:294-400`

**Related to #1** — current code buffers nothing but re-reads. Once #1 is fixed, ensure streaming write with `std::ofstream::write` in large chunks (already done in fix).

---

### 9. CALIBRATION MASTER ACQUISITION: PARALLELIZE FRAME AVERAGING — MEDIUM

**Location:** `calibration.cpp:95-155` (`MasterAcquisition::run`)

**Problem:** Averages N frames sequentially in single thread. Each frame: expose → wait → read → accumulate.

**Fix:**
- Use `std::execution::par` or OpenMP for accumulation (if camera allows concurrent reads — it doesn't, but accumulation is independent)
- Better: overlap exposure wait with previous frame accumulation (pipeline)

```cpp
// Double-buffer: while waiting for frame N, accumulate frame N-1
Frame raw1, raw2;
bool use1 = true;
for (int i = 0; i < total_; ++i) {
    auto& cur = use1 ? raw1 : raw2;
    camera.startExposure(false);
    // Accumulate previous while exposing
    if (i > 0) {
        #pragma omp simd
        for (k) accum[k] += prev_raw[k];
    }
    wait_for_exposure();
    camera.getDataAfterExp(cur.data);
    use1 = !use1;
}
// Final accumulation
#pragma omp simd
for (k) accum[k] += (use1 ? raw2 : raw1)[k];
```

**Impact:** Hides accumulation latency behind exposure wait.

---

### 10. GPU ACCELERATION PATH — STRATEGIC

**Where it matters:**
- Calibration apply (per-pixel independent) → **CUDA/OpenCL/Metal compute shader**
- Preview generation (box filter + stretch) → **compute shader**
- Histogram → **GPU reduction**
- PCA / dimensionality reduction on cube → **cuSOLVER / MKL**

**Implementation approach:**
```cpp
// Abstract interface
class ImageProcessor {
public:
    virtual void calibrate(const float* raw, const float* dark, const float* flat,
                           float* out, int w, int h) = 0;
    virtual void boxDownsample(const float* in, float* out, int w, int h, int scale) = 0;
    virtual void stretchToU8(const float* in, uint8_t* out, int n, float lo, float hi) = 0;
    virtual ~ImageProcessor() = default;
};

// CPU implementation (current)
class CpuImageProcessor : public ImageProcessor { ... };

// GPU implementation (CUDA)
#ifdef HAVE_CUDA
class CudaImageProcessor : public ImageProcessor { ... };
#endif
```

**Integration:** Factory selects based on hardware. GUI gets calibrated preview via GPU → direct GL interop (CUDA-GL) for zero-copy display.

---

## Memory & Data Handling Issues

| Issue | Location | Fix |
|-------|----------|-----|
| `CubeBand.data` = `vector<float>` per band in memory | `cube.h:24` | Already bounded by preview (5MB cap). OK. |
| `saveEnvi()` loads one band at a time | `cube.cpp:329` | Fixed by #1 (stream during acquisition) |
| `CalibrationManager` holds full dark+flat in RAM | `calibration.h:147-148` | OK (2 × 4.2M × 4B = 34MB) |
| `CaptureThread` double-buffers full frame | `capture_thread.h:58-60` | OK for preview |
| `ImageWriter::ByteStream` buffers full TIFF | `image_writer.cpp:137` | Fixed by #7 |

---

## Parallelization & Hardware Utilization Gaps

| Opportunity | Current | Recommended |
|-------------|---------|-------------|
| **Multi-core calibration** | Single-threaded | OpenMP `#pragma omp parallel for simd` |
| **Multi-core preview** | Single-threaded | OpenMP on `makePreviewBand` passes |
| **GPU calibration** | None | CUDA/Metal compute shader |
| **GPU preview** | CPU → GL upload | Compute shader + GL interop |
| **Async I/O** | Blocking `ofstream` | `io_uring` (Linux) / overlapped I/O (Win) / `dispatch_io` (macOS) |
| **Pipelined acquisition** | Sequential | Producer-consumer (mono mover + camera worker) |

---

## Algorithmic Optimizations

| Area | Current | Better Approach |
|------|---------|-----------------|
| **Spectral smoothing** | None | Savitzky-Golay filter on spectra (per-pixel) before PCA |
| **Dimensionality reduction** | None | Incremental PCA / randomized SVD on cube for denoising |
| **Bad pixel correction** | None | Median filter on spatial neighbors per band |
| **Spectral calibration** | Wavelength from monochromator only | Fit polynomial to known emission lines |
| **Flat-field normalization** | Mean(flat-dark) scalar | Per-pixel gain map (already done) but vectorize |

---

## Code Architecture & Modularity Issues

| Issue | Location | Recommendation |
|-------|----------|----------------|
| **God-class `Gui`** | `gui.cpp` 2000+ lines | Split: `CameraPanel`, `ScanPanel`, `CubeViewer`, `CalibrationPanel` |
| **Tight coupling: `Gui` owns `Camera`, `Mono`, `CubeSession`, `Calib`** | `gui.h` | Dependency injection; `Gui` should only coordinate |
| **`ScanThread` does acquisition + calibration + I/O + preview** | `cube.cpp:442-699` | Separate: `AcquisitionEngine`, `CalibrationPipeline`, `DataWriter` |
| **`ImageWriter` = TIFF + FITS + PNG + FloatTIFF + ENVI** | `image_writer.cpp` | Split per format; strategy pattern |
| **No abstraction for camera SDK** | `camera.cpp` | `ICamera` interface → testable, mockable |
| **Serial protocol hardcoded in `Monochromator`** | `monochromator.cpp` | Protocol abstraction for different firmware |

---

## I/O and Data Pipeline Efficiency

| Problem | Fix |
|---------|-----|
| **Per-band TIFF write blocks scan loop** | Async writer thread + ring buffer |
| **ENVI re-reads from disk** | Stream during acquisition (#1) |
| **No compression option** | Add BLOSC/ZSTD for float TIFF / ENVI |
| **No chunked reading for large cubes** | `CubeSession::loadFromDir` loads all previews — add lazy loading |
| **Metadata JSON written as text** | Keep JSON (human-readable), but consider binary sidecar for large cubes |

---

## Low-Level Optimization Candidates for C/C++/Rust/JIT

| Target | Reason | Approach |
|--------|--------|----------|
| `CalibrationManager::apply` | Hot path, memory-bound, SIMD-friendly | Keep C++, add `#pragma omp simd` + `-march=native` |
| `makePreviewBand` | O(scale²) → separable + SIMD | C++ with OpenMP |
| `computeHistogram` | Full-frame, repeated | GPU compute shader or SIMD CPU |
| `ImageWriter::toGrey16` | Per-frame conversion | SIMD + avoid branch |
| **Spectral operations (PCA, smoothing)** | Not implemented yet | **Python/Numba** for prototyping → **C++/Eigen** for production |

---

## Concrete Refactoring Suggestions (Before/After)

### A. `ScanThread` → Pipeline Architecture

**Before:** Monolithic `run()` with sequential steps.

**After:**
```cpp
// acquisition_engine.h
class AcquisitionEngine {
public:
    struct Config { ScanParams params; CalibrationManager* calib; };
    using BandCallback = std::function<void(const CubeBand&, bool isLast)>;
    
    AcquisitionEngine(Camera& cam, Monochromator& mono, Config cfg);
    void start(BandCallback onBand);  // async, calls back per band
    void cancel();
    bool isRunning() const;
private:
    // Internal: mover thread, worker thread, ring buffer
};

// scan_thread.cpp (simplified)
void ScanThread::run() {
    engine_.start([this](const CubeBand& band, bool last) {
        session_->appendBand(makePreviewBand(band));
        if (last) {
            status_ = Status::Exporting;
            session_->saveMetadata();
            session_->saveEnvi();  // now just writes .hdr
            status_ = Status::Done;
        }
    });
}
```

### B. `CalibrationManager` → SIMD + Fused Ops

**Before:** 3-pass scalar loops.

**After:**
```cpp
// calibration.cpp — single fused kernel
AppliedFrame CalibrationManager::apply(const Frame& raw) const {
    AppliedFrame out;
    out.width = raw.width;
    out.height = raw.height;
    size_t n = (size_t)raw.width * raw.height;
    out.data.resize(n);
    
    // Load raw → float (SIMD)
    if (raw.type == ASI_IMG_RAW16) {
        const uint16_t* src = (const uint16_t*)raw.data.data();
        #pragma omp simd
        for (size_t i = 0; i < n; ++i) out.data[i] = src[i];
    } else {
        #pragma omp simd
        for (size_t i = 0; i < n; ++i) out.data[i] = raw.data[i];
    }
    
    // Fused calibrate in-place
    if (dark_ && flat_ && dark_->data.size() == n && flat_->data.size() == n) {
        double sum = 0;
        #pragma omp simd reduction(+:sum)
        for (size_t i = 0; i < n; ++i) sum += flat_->data[i] - dark_->data[i];
        double meanFMD = sum / n;
        
        #pragma omp simd
        for (size_t i = 0; i < n; ++i) {
            float fmd = flat_->data[i] - dark_->data[i];
            fmd = fmd <= 1e-6f ? 1e-6f : fmd;
            out.data[i] = (out.data[i] - dark_->data[i]) / fmd * (float)meanFMD;
        }
    } else if (dark_ && dark_->data.size() == n) {
        #pragma omp simd
        for (size_t i = 0; i < n; ++i) out.data[i] -= dark_->data[i];
    }
    return out;
}
```

### C. `ImageWriter` → Streaming + Format Separation

**Before:** Single class, buffers entire file.

**After:**
```cpp
// image_writer.h
class ITiffWriter {
public:
    virtual void writeHeader(std::ostream&, int w, int h, const std::string& desc) = 0;
    virtual void writeStrip(std::ostream&, const void* pixels, size_t bytes) = 0;
    virtual ~ITiffWriter() = default;
};

class FloatTiffWriter : public ITiffWriter { ... };
class UInt16TiffWriter : public ITiffWriter { ... };

// Usage:
std::ofstream f(path, std::ios::binary);
writer.writeHeader(f, w, h, desc);
for (chunks) writer.writeStrip(f, chunkPtr, chunkBytes);
```

---

## Recommended Libraries / Tools

| Need | Recommendation |
|------|----------------|
| **SIMD intrinsics helper** | `xsimd` (header-only, cross-platform) |
| **Parallel algorithms** | OpenMP (already in C++17) or `oneTBB` |
| **GPU compute** | CUDA (NVIDIA), Metal (Apple), SYCL/oneAPI (cross-vendor) |
| **Async I/O** | `liburing` (Linux), `IOCP` (Win), `dispatch_io` (macOS) — or `boost::asio` |
| **Compression** | `blosc2` (fast, scientific data), `zstd` |
| **ENVI/HDF5** | `GDAL` (production) or custom minimal writer |
| **PCA/SVD** | `Eigen` (CPU), `cuSOLVER` (GPU) |
| **Profiling** | `perf` + `hotspot`, `VTune`, `Instruments` (macOS) |

---

## Quick Wins (Do First)

1. **`saveEnvi()` streaming** — 1 hour, massive I/O reduction
2. **`-O3 -march=native -ftree-vectorize` in CMake** — free 2-3× on loops
3. **`#pragma omp simd` on calibration + preview loops** — 3-5×
4. **Persistent buffers in `uploadFrameToTexture`** — removes alloc pressure
5. **Separable box filter in `makePreviewBand`** — 4× faster preview

---

## Strategic Direction

| Timeline | Focus |
|----------|-------|
| **Week 1-2** | Quick wins above + pipelined acquisition |
| **Month 1** | GPU calibration + preview (Metal on macOS, CUDA on Linux) |
| **Month 2** | Async I/O writer + compressed ENVI (BLOSC) |
| **Month 3** | Modular architecture refactor (testability, headless CLI) |
| **Ongoing** | Spectral processing algorithms (denoising, PCA, classification) |

---

This pipeline has solid foundations (clean C++, RAII, threaded design) but **leaves 5-10× performance on the table** from scalar loops, redundant I/O, and missed parallelism. The fixes above are mechanically straightforward — no algorithmic research needed.


| Likewise: |  |
| --- | --- |
| Probability (Dimensionless) |  |
| dP |  |
| ​ |  |
|   |  |
| ​ |  |
|  =  |  |
| Density (Probability per unit x) |  |
| f(x) |  |
| ​ |  |
|   |  |
| ​ |  |
|  ×  |  |
| Interval Width (Units of x) |  |
| dx |  |
| ​ |  |
|   |  |
| ​ |  |
|   |  |
| Dimensional Consistency |  |
