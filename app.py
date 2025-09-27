import os
import io
import tempfile
import shutil
import subprocess
from flask import Flask, request, send_file, jsonify, abort
from werkzeug.utils import secure_filename

app = Flask(__name__)
# If you don't already have this:
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # allow cross-origin requests from your frontend; tighten in prod

@app.route("/health")
def health():
    return "OK", 200


# Configuration
MAX_UPLOAD_MB = 200  # max upload size
ALLOWED_EXTENSIONS = {'pdf'}
GS_BINARY_CANDIDATES = ['gs', 'gswin64c', 'gswin32c']
MAX_ITERATIONS = 8   # binary search iterations
MIN_DPI = 72         # don't go below this dpi usually
DEFAULT_TIMEOUT = 60  # seconds for gs subprocess

def find_gs():
    """Find Ghostscript binary on system."""
    for name in GS_BINARY_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("Ghostscript binary not found. Please install Ghostscript and ensure `gs` is in PATH.")

GS_BIN = None
try:
    GS_BIN = find_gs()
except Exception as e:
    GS_BIN = None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def file_size_mb(file_stream):
    file_stream.seek(0, os.SEEK_END)
    size = file_stream.tell()
    file_stream.seek(0)
    return size / (1024 * 1024), size

def compress_with_gs(input_path, output_path, dpi, pdfsettings='/printer', timeout=DEFAULT_TIMEOUT):
    """
    Run Ghostscript to downsample images to `dpi`.
    pdfsettings is used mainly for compatibility; we'll also set explicit downsampling args.
    """
    args = [
        GS_BIN,
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={pdfsettings}",  # affects image compression defaults
        "-dAutoRotatePages=/None",

        # Force downsampling to given DPI
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",
        f"-dColorImageResolution={int(dpi)}",
        f"-dGrayImageResolution={int(dpi)}",
        f"-dMonoImageResolution={int(dpi)}",
        "-dDetectDuplicateImages=true",
        "-dEmbedAllFonts=true",
        "-dSubsetFonts=true",
        f"-sOutputFile={output_path}",
        input_path
    ]
    subprocess.run(args, check=True, timeout=timeout)


@app.route("/compress", methods=["POST"])
def compress_endpoint():
    if GS_BIN is None:
        return jsonify({"error": "Ghostscript not available on server"}), 500

    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type; PDF required"}), 400

    # size check
    mb, orig_bytes = file_size_mb(file.stream)
    if mb > MAX_UPLOAD_MB:
        return jsonify({"error": f"File too large. Max allowed {MAX_UPLOAD_MB} MB"}), 400

    quality = request.form.get('quality', 'high').lower()
    if quality not in ('high', 'medium', 'low'):
        quality = 'high'

    target_size_mb = request.form.get('targetSizeMB')
    try:
        target_size_mb = float(target_size_mb) if target_size_mb else None
    except:
        return jsonify({"error": "Invalid targetSizeMB"}), 400

    # If no target specified, just compress based on quality presets (single-pass)
    # Map quality to starting DPI and PDFSETTINGS
    quality_map = {
        'high': {'dpi': 300, 'pdfsettings': '/prepress'},
        'medium': {'dpi': 200, 'pdfsettings': '/printer'},
        'low': {'dpi': 150, 'pdfsettings': '/ebook'}
    }
    start_dpi = quality_map[quality]['dpi']
    pdfsettings = quality_map[quality]['pdfsettings']

    filename = secure_filename(file.filename)

    # If target size is None or target >= original => single pass or return original
    if target_size_mb is None or target_size_mb >= mb:
        # If target is None: do one pass at the quality DPI
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = os.path.join(tmpdir, filename)
            out_path = os.path.join(tmpdir, f"compressed_{filename}")
            file.stream.seek(0)
            with open(in_path, "wb") as f:
                f.write(file.stream.read())

            try:
                compress_with_gs(in_path, out_path, start_dpi, pdfsettings=pdfsettings)
            except subprocess.CalledProcessError as e:
                return jsonify({"error": "Ghostscript failed", "details": str(e)}), 500
            except Exception as e:
                return jsonify({"error": "Compression error", "details": str(e)}), 500

            # Return result
            with open(out_path, "rb") as fh:
                compressed_bytes = fh.read()
            compressed_size = len(compressed_bytes)

            headers = {
                "X-Original-Size": str(orig_bytes),
                "X-Compressed-Size": str(compressed_size),
                "X-Compression-Ratio": f"{compressed_size / orig_bytes:.4f}",
                "X-Quality-Used": quality,
            }
            if target_size_mb:
                headers["X-Target-Size"] = str(target_size_mb)

            return send_file(
                io.BytesIO(compressed_bytes),
                mimetype="application/pdf",
                download_name=f"compressed_{filename}",
                as_attachment=True,
                headers=headers
            )

    # If we have a target < original, attempt iterative binary search on DPI to reach target
    # Binary search DPI between MIN_DPI and start_dpi
    low = MIN_DPI
    high = start_dpi
    best_candidate = None
    best_size_diff = float('inf')
    best_dpi = None

    # Save uploaded file to temp
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, filename)
        file.stream.seek(0)
        with open(in_path, "wb") as f:
            f.write(file.stream.read())

        # quick sanity: if target > orig, send original (should be handled above)
        target_bytes = int(target_size_mb * 1024 * 1024)

        for i in range(MAX_ITERATIONS):
            mid = (low + high) / 2.0
            out_path = os.path.join(tmpdir, f"out_{int(mid)}.pdf")
            try:
                compress_with_gs(in_path, out_path, mid, pdfsettings=pdfsettings)
            except subprocess.CalledProcessError as e:
                # stop and return last best
                break
            except Exception as e:
                break

            candidate_size = os.path.getsize(out_path)
            diff = candidate_size - target_bytes

            # record best (closest to target but not necessarily under)
            if abs(diff) < best_size_diff:
                best_size_diff = abs(diff)
                best_candidate = out_path
                best_dpi = int(mid)

            # If exactly equal (within a small tolerance), break
            if abs(diff) <= 1024 * 10:  # within 10 KB
                break

            # If candidate larger than target -> reduce dpi (more compression)
            if candidate_size > target_bytes:
                high = mid  # try lower DPI
            else:
                # candidate smaller than target -> can try increasing DPI to get better quality
                low = mid

            # break if search range already small
            if (high - low) < 1.0:
                break

        # if best_candidate not set (e.g. early failure), fallback to single-pass at low DPI
        if best_candidate is None:
            try:
                fallback_out = os.path.join(tmpdir, f"fallback_{filename}")
                compress_with_gs(in_path, fallback_out, MIN_DPI, pdfsettings=pdfsettings)
                best_candidate = fallback_out
                best_dpi = MIN_DPI
            except Exception as e:
                return jsonify({"error": "Compression failed", "details": str(e)}), 500

        # read result
        with open(best_candidate, "rb") as fh:
            compressed_bytes = fh.read()

        compressed_size = len(compressed_bytes)

        headers = {
            "X-Original-Size": str(orig_bytes),
            "X-Compressed-Size": str(compressed_size),
            "X-Compression-Ratio": f"{compressed_size / orig_bytes:.4f}",
            "X-Quality-Used": quality,
            "X-Target-Size": str(target_size_mb)
        }

        # send result
        return send_file(
            io.BytesIO(compressed_bytes),
            mimetype="application/pdf",
            download_name=f"compressed_{filename}",
            as_attachment=True,
            headers=headers
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
