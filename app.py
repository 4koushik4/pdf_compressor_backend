import os
import io
import tempfile
import shutil
import subprocess
from flask import Flask, request, send_file, jsonify
from werkzeug.utils import secure_filename
from flask_cors import CORS

# Flask app
app = Flask(__name__)
CORS(app, origins=["https://zenpdf.vercel.app"], supports_credentials=True)

# Health check
@app.route("/health")
def health():
    return "OK", 200

# Configuration
MAX_UPLOAD_MB = 200
ALLOWED_EXTENSIONS = {'pdf'}
GS_BINARY_CANDIDATES = ['gs', 'gswin64c', 'gswin32c']
MAX_ITERATIONS = 8
MIN_DPI = 72
DEFAULT_TIMEOUT = 60  # seconds

def find_gs():
    for name in GS_BINARY_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("Ghostscript binary not found. Please install it and ensure `gs` is in PATH.")

GS_BIN = None
try:
    GS_BIN = find_gs()
except Exception:
    GS_BIN = None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def file_size_mb(file_stream):
    file_stream.seek(0, os.SEEK_END)
    size = file_stream.tell()
    file_stream.seek(0)
    return size / (1024 * 1024), size

def compress_with_gs(input_path, output_path, dpi, pdfsettings='/printer', timeout=DEFAULT_TIMEOUT):
    args = [
        GS_BIN,
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={pdfsettings}",
        "-dAutoRotatePages=/None",
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

    quality_map = {
        'high': {'dpi': 300, 'pdfsettings': '/prepress'},
        'medium': {'dpi': 200, 'pdfsettings': '/printer'},
        'low': {'dpi': 150, 'pdfsettings': '/ebook'}
    }
    start_dpi = quality_map[quality]['dpi']
    pdfsettings = quality_map[quality]['pdfsettings']

    filename = secure_filename(file.filename)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, filename)
        file.stream.seek(0)
        with open(in_path, "wb") as f:
            f.write(file.stream.read())

        # Single pass if no target or target >= original
        if target_size_mb is None or target_size_mb >= mb:
            out_path = os.path.join(tmpdir, f"compressed_{filename}")
            try:
                compress_with_gs(in_path, out_path, start_dpi, pdfsettings=pdfsettings)
            except Exception as e:
                return jsonify({"error": "Compression error", "details": str(e)}), 500

            with open(out_path, "rb") as fh:
                compressed_bytes = fh.read()
            compressed_size = len(compressed_bytes)

            response = send_file(
                io.BytesIO(compressed_bytes),
                mimetype="application/pdf",
                download_name=f"compressed_{filename}",
                as_attachment=True
            )

            # Set custom headers (Flask 3.x compatible)
            response.headers["X-Original-Size"] = str(orig_bytes)
            response.headers["X-Compressed-Size"] = str(compressed_size)
            response.headers["X-Compression-Ratio"] = f"{compressed_size / orig_bytes:.4f}"
            response.headers["X-Quality-Used"] = quality
            if target_size_mb:
                response.headers["X-Target-Size"] = str(target_size_mb)

            return response

        # Iterative compression for target size
        low = MIN_DPI
        high = start_dpi
        best_candidate = None
        best_size_diff = float('inf')
        best_dpi = None
        target_bytes = int(target_size_mb * 1024 * 1024)

        for i in range(MAX_ITERATIONS):
            mid = (low + high) / 2.0
            out_path = os.path.join(tmpdir, f"out_{int(mid)}.pdf")
            try:
                compress_with_gs(in_path, out_path, mid, pdfsettings=pdfsettings)
            except Exception:
                break

            candidate_size = os.path.getsize(out_path)
            diff = candidate_size - target_bytes
            if abs(diff) < best_size_diff:
                best_size_diff = abs(diff)
                best_candidate = out_path
                best_dpi = int(mid)
            if abs(diff) <= 1024 * 10:
                break
            if candidate_size > target_bytes:
                high = mid
            else:
                low = mid
            if (high - low) < 1.0:
                break

        if best_candidate is None:
            fallback_out = os.path.join(tmpdir, f"fallback_{filename}")
            try:
                compress_with_gs(in_path, fallback_out, MIN_DPI, pdfsettings=pdfsettings)
                best_candidate = fallback_out
                best_dpi = MIN_DPI
            except Exception as e:
                return jsonify({"error": "Compression failed", "details": str(e)}), 500

        with open(best_candidate, "rb") as fh:
            compressed_bytes = fh.read()
        compressed_size = len(compressed_bytes)

        response = send_file(
            io.BytesIO(compressed_bytes),
            mimetype="application/pdf",
            download_name=f"compressed_{filename}",
            as_attachment=True
        )

        response.headers["X-Original-Size"] = str(orig_bytes)
        response.headers["X-Compressed-Size"] = str(compressed_size)
        response.headers["X-Compression-Ratio"] = f"{compressed_size / orig_bytes:.4f}"
        response.headers["X-Quality-Used"] = quality
        response.headers["X-Target-Size"] = str(target_size_mb) if target_size_mb else ""

        return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
