from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageOps, ImageTk


APP_NAME = "KOMaPa"
SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
TARGET_SIZE = (600, 800)


def detect_panels(image: Image.Image) -> list[tuple[float, float, float, float]]:
    """Split on clear gutters or continuous frame lines, preserving manga reading order."""
    original_w, original_h = image.size
    scale = min(1.0, 1500 / max(original_w, original_h))
    gray = image.convert("L")
    if scale < 1:
        gray = gray.resize((max(1, round(original_w * scale)), max(1, round(original_h * scale))), Image.Resampling.BILINEAR)
    pixels = np.asarray(gray)
    paper = int(np.clip(np.percentile(pixels, 99) - 8, 185, 245))
    page_h, page_w = pixels.shape
    boxes: list[tuple[int, int, int, int]] = []
    unsafe_split = False

    def find_gutter(rect: tuple[int, int, int, int], horizontal: bool) -> tuple[int, int] | None:
        x1, y1, x2, y2 = rect
        region = pixels[y1:y2, x1:x2]
        length = y2 - y1 if horizontal else x2 - x1
        cross = x2 - x1 if horizontal else y2 - y1
        if length < 80 or cross < 40:
            return None
        axis = 1 if horizontal else 0
        light_coverage = (region >= paper).mean(axis=axis)
        dark_coverage = (region <= 36).mean(axis=axis)
        edge = max(2, int(length * 0.035))
        min_light_run = max(4, int(length * 0.0045))
        # At preview scale a printed frame can be only one pixel thick.
        min_dark_run = max(1, int(length * 0.001))
        support_width = max(3, int(length * 0.015))
        best: tuple[int, int, float, str, float] | None = None
        for coverage, threshold, min_run, kind in (
            # Require a strong separator. If it is too interrupted, keep the
            # current region intact rather than risk cutting through artwork.
            (light_coverage, 0.96,
             max(4, int(length * (0.003 if not horizontal else 0.0045))), "light"),
            (dark_coverage, 0.96, min_dark_run, "dark"),
        ):
            # Black backgrounds leave long dark runs between letters and objects.
            # Only count a dark divider when it is surrounded by substantially brighter art.
            if kind == "dark" and float(np.median(region)) < 70:
                continue
            i, stop = edge, length - edge
            while i < stop:
                if coverage[i] < threshold:
                    i += 1
                    continue
                start = i
                last_good = i
                i += 1
                while i < stop:
                    if coverage[i] >= threshold:
                        last_good = i
                        i += 1
                    else:
                        break
                end = last_good + 1
                center = (start + end) // 2
                min_position = 0.12 if horizontal else 0.18
                max_position = 0.88 if horizontal else 0.82
                if end - start < min_run or center < length * min_position or center > length * max_position:
                    i = max(i, end)
                    continue

                before_start, before_end = max(0, start - support_width), start
                after_start, after_end = end, min(length, end + support_width)
                if horizontal:
                    before = region[before_start:before_end, :]
                    after = region[after_start:after_end, :]
                else:
                    before = region[:, before_start:before_end]
                    after = region[:, after_start:after_end]
                if not before.size or not after.size:
                    i = max(i, end)
                    continue
                # A real separator should have image detail on both sides, not just blank paper.
                if kind == "dark":
                    contrast_threshold = max(55, int(paper * 0.35))
                    before_detail = float((before >= contrast_threshold).mean())
                    after_detail = float((after >= contrast_threshold).mean())
                    min_support = 0.20
                else:
                    before_detail = float((before < paper).mean())
                    after_detail = float((after < paper).mean())
                    min_support = 0.004
                if min(before_detail, after_detail) < min_support:
                    i = max(i, end)
                    continue
                purity = float(coverage[start:end].mean())
                score = (end - start) * purity * (1.0 + min(before_detail, after_detail))
                if best is None or score > best[2]:
                    best = (start, end, score, kind, purity)
                i = max(i, end)
        return None if best is None else (best[0], best[1], best[3], best[4])

    def split(rect: tuple[int, int, int, int], depth: int = 0) -> None:
        nonlocal unsafe_split
        x1, y1, x2, y2 = rect
        if depth >= 12 or x2 - x1 < 48 or y2 - y1 < 64:
            boxes.append(rect)
            return
        # Manga order: split rows first, then traverse each row right to left.
        # Short text strips are captions, not a sequence of narrow panels.
        gutter = find_gutter(rect, True) if (x2 - x1) >= page_w * 0.18 else None
        if gutter:
            cut1, cut2 = y1 + gutter[0], y1 + gutter[1]
            upper_area = (x2 - x1) * (cut1 - y1)
            lower_area = (x2 - x1) * (y2 - cut2)
            min_area = max(4000, page_w * page_h * 0.025)
            if cut1 - y1 >= max(36, page_h * 0.07) and y2 - cut2 >= max(36, page_h * 0.07) and min(upper_area, lower_area) >= min_area:
                if (gutter[2] == "light" and gutter[3] < 0.99
                        and (gutter[1] - gutter[0]) / max(1, y2 - y1) <= 0.015):
                    unsafe_split = True
                    boxes.append(rect)
                    return
                # Let both crops include the detected separator band. This
                # preserves panel frame strokes at the crop edges.
                split((x1, y1, x2, cut2), depth + 1)
                split((x1, cut1, x2, y2), depth + 1)
                return
        # Don't look for vertical gutters inside a short title or dialogue row.
        gutter = find_gutter(rect, False) if (y2 - y1) >= page_h * 0.14 else None
        if gutter:
            cut1, cut2 = x1 + gutter[0], x1 + gutter[1]
            left_area = (cut1 - x1) * (y2 - y1)
            right_area = (x2 - cut2) * (y2 - y1)
            min_area = max(4000, page_w * page_h * 0.025)
            if cut1 - x1 >= max(36, page_w * 0.18) and x2 - cut2 >= max(36, page_w * 0.18) and min(left_area, right_area) >= min_area:
                # A separator with visible interruptions may contain a drawing
                # crossing both panels (for example a paper plane). Keep the
                # whole page in that case rather than risk cutting the artwork.
                if (gutter[2] == "light" and gutter[3] < 0.99
                        and (gutter[1] - gutter[0]) / max(1, x2 - x1) <= 0.015):
                    unsafe_split = True
                    boxes.append(rect)
                    return
                split((cut1, y1, x2, y2), depth + 1)
                split((x1, y1, cut2, y2), depth + 1)
                return
        boxes.append(rect)

    def fallback_into_two() -> list[tuple[float, float, float, float]] | None:
        """Use one strong page-wide gutter to form two safe reading blocks."""
        min_area = max(4000, page_w * page_h * 0.025)
        # Manga pages are usually arranged in horizontal bands, so prefer a
        # full-width horizontal gutter. Try a full-height vertical gutter only
        # when no safe horizontal division exists.
        for horizontal in (True, False):
            gutter = find_gutter((0, 0, page_w, page_h), horizontal)
            if not gutter:
                continue
            cut1, cut2 = gutter[0], gutter[1]
            # Broad blank areas between text columns are not reliable panel
            # borders. A fallback split must follow a relatively narrow gutter.
            axis_length = page_h if horizontal else page_w
            if cut2 - cut1 > axis_length * 0.05:
                continue
            if horizontal:
                first_size, second_size = cut1, page_h - cut2
                first_area, second_area = page_w * first_size, page_w * second_size
                candidates = [(0, 0, page_w, cut2), (0, cut1, page_w, page_h)]
            else:
                first_size, second_size = cut1, page_w - cut2
                first_area, second_area = first_size * page_h, second_size * page_h
                # Right-to-left manga reading order.
                candidates = [(cut1, 0, page_w, page_h), (0, 0, cut2, page_h)]
            total_size = page_h if horizontal else page_w
            if (first_size < max(36, total_size * 0.07)
                    or second_size < max(36, total_size * 0.07)
                    or min(first_area, second_area) < min_area):
                continue
            sx, sy = original_w / page_w, original_h / page_h
            return [(x1 * sx, y1 * sy, x2 * sx, y2 * sy)
                    for x1, y1, x2, y2 in candidates]
        return None

    split((0, 0, page_w, page_h))

    # If any proposed cut shows signs of crossing artwork, the segmentation
    # for this page is no longer trustworthy. Try one page-wide gutter before
    # giving up and keeping the whole page.
    if unsafe_split:
        two_blocks = fallback_into_two()
        if two_blocks:
            return two_blocks
        return [(0.0, 0.0, float(original_w), float(original_h))]

    # A thin strip above or below a full-width panel is often part of that
    # panel (for example, a character's speech/thought tail or an intentionally
    # tall frame split by a horizontal gutter). Join only short horizontal
    # fragments that closely align with a much taller vertical neighbor.
    max_strip_height = page_h * 0.10
    max_join_gap = max(8, int(page_h * 0.025))
    changed = True
    while changed:
        changed = False
        for i, first in enumerate(boxes):
            ax1, ay1, ax2, ay2 = first
            a_height = ay2 - ay1
            if a_height > max_strip_height:
                continue
            for j, second in enumerate(boxes):
                if i == j:
                    continue
                bx1, by1, bx2, by2 = second
                b_height = by2 - by1
                if b_height < a_height * 2.0:
                    continue
                overlap = max(0, min(ax2, bx2) - max(ax1, bx1))
                if overlap / max(1, min(ax2 - ax1, bx2 - bx1)) < 0.85:
                    continue
                if ay2 <= by1:
                    gap = by1 - ay2
                elif by2 <= ay1:
                    gap = ay1 - by2
                else:
                    continue
                if gap > max_join_gap:
                    continue

                # Keep small panels separate when a solid black frame line
                # marks their shared edge. The short-strip merge is meant for
                # accidental slices through one panel, not real framed panels.
                shared_x1, shared_x2 = max(ax1, bx1), min(ax2, bx2)
                frame_margin = max(3, int(page_h * 0.01))
                if ay2 <= by1:
                    separator = pixels[max(0, ay2 - frame_margin):min(page_h, by1 + frame_margin), shared_x1:shared_x2]
                else:
                    separator = pixels[max(0, by2 - frame_margin):min(page_h, ay1 + frame_margin), shared_x1:shared_x2]
                if separator.size and float((separator <= 36).mean(axis=1).max()) >= 0.90:
                    continue

                merged = (min(ax1, bx1), min(ay1, by1), max(ax2, bx2), max(ay2, by2))
                first_index, second_index = sorted((i, j))
                boxes[first_index] = merged
                boxes.pop(second_index)
                changed = True
                break
            if changed:
                break

    # Combine up to three small, side-by-side panels when they form one row.
    # This prevents narrow Kindle crops while leaving large panels independent.
    max_small_area = page_w * page_h * 0.35
    min_group_width = page_w * 0.32
    max_row_gap = max(8, int(page_w * 0.06))
    row_tolerance = max(6, int(page_h * 0.02))
    boxes.sort(key=lambda b: (b[1], b[0]))
    grouped: list[tuple[int, int, int, int]] = []
    grouped_counts: list[int] = []
    grouped_largest_area: list[int] = []
    grouped_narrowest_width: list[int] = []
    for box in boxes:
        if grouped:
            prev = grouped[-1]
            px1, py1, px2, py2 = prev
            x1, y1, x2, y2 = box
            area = (x2 - x1) * (y2 - y1)
            vertically_aligned = abs(py1 - y1) <= row_tolerance and abs(py2 - y2) <= row_tolerance
            # Adjacent crops may overlap by the separator width because both
            # crops retain the frame at their shared edge.
            adjacent = abs(x1 - px2) <= max_row_gap
            if (vertically_aligned and adjacent and grouped_largest_area[-1] <= max_small_area and area <= max_small_area
                    and grouped_narrowest_width[-1] >= min_group_width and x2 - x1 >= min_group_width
                    and grouped_counts[-1] < 3):
                grouped[-1] = (px1, min(py1, y1), x2, max(py2, y2))
                grouped_counts[-1] += 1
                grouped_largest_area[-1] = max(grouped_largest_area[-1], area)
                grouped_narrowest_width[-1] = min(grouped_narrowest_width[-1], x2 - x1)
                continue
        grouped.append(box)
        grouped_counts.append(1)
        grouped_largest_area.append((box[2] - box[0]) * (box[3] - box[1]))
        grouped_narrowest_width.append(box[2] - box[0])

    # Restore manga reading order: top to bottom, right to left.
    grouped.sort(key=lambda b: (b[1], -b[2]))

    # A valid panel layout may share a thin frame band, but its crops should
    # not overlap substantially. Large intersections usually mean that text
    # columns or artwork were mistaken for independent gutters; preserve the
    # page intact whenever the proposed layout is geometrically ambiguous.
    for index, first in enumerate(grouped):
        ax1, ay1, ax2, ay2 = first
        first_area = max(1, (ax2 - ax1) * (ay2 - ay1))
        for second in grouped[index + 1:]:
            bx1, by1, bx2, by2 = second
            intersection = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
            second_area = max(1, (bx2 - bx1) * (by2 - by1))
            if intersection / min(first_area, second_area) > 0.12:
                two_blocks = fallback_into_two()
                if two_blocks:
                    return two_blocks
                return [(0.0, 0.0, float(original_w), float(original_h))]

    # Limit every source page to at most three reading blocks. If individual
    # detection yields more, retain only a safe page-wide division into two;
    # otherwise use the complete page rather than creating many uncertain cuts.
    if len(grouped) > 3:
        two_blocks = fallback_into_two()
        if two_blocks:
            return two_blocks
        return [(0.0, 0.0, float(original_w), float(original_h))]

    sx, sy = original_w / page_w, original_h / page_h
    return [(x1 * sx, y1 * sy, x2 * sx, y2 * sy) for x1, y1, x2, y2 in grouped]


def natural_key(path: str) -> list[object]:
    import re

    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path)]


def find_7zip() -> str | None:
    for name in ("7z", "7zz", "7za", "7z.exe"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "7-Zip" / "7z.exe",
    ]
    return next((str(path) for path in candidates if path.exists()), None)


def create_extraction_tempdir() -> tempfile.TemporaryDirectory[str]:
    # Keep the extraction writable in restricted desktop environments; fall back to OS temp.
    for parent in (Path(__file__).resolve().parent, Path(tempfile.gettempdir())):
        try:
            return tempfile.TemporaryDirectory(prefix="manga_panels_", dir=parent)
        except OSError:
            continue
    raise OSError("No se pudo crear una carpeta temporal para abrir el manga.")


class MangaPanelApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1100x820")
        self.root.minsize(780, 600)
        self.root.configure(bg="#f4f1ec")

        self.archive: Path | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.pages: list[Path] = []
        self.page_index = 0
        self.crops: dict[int, list[tuple[float, float, float, float]]] = {}
        self.photo: ImageTk.PhotoImage | None = None
        self.source_image: Image.Image | None = None
        self.display_scale = 1.0
        self.display_offset = (0, 0)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 9))

        header = tk.Frame(self.root, bg="#f4f1ec", padx=24, pady=18)
        header.pack(fill="x")
        tk.Label(header, text="KOMaPa", font=("Segoe UI", 22, "bold"), bg="#f4f1ec", fg="#20201e").pack(anchor="w")
        tk.Label(header, text="Detecta las viñetas automáticamente y crea un CBZ para KOReader.", font=("Segoe UI", 10), bg="#f4f1ec", fg="#68645e").pack(anchor="w", pady=(3, 0))

        toolbar = tk.Frame(self.root, bg="#f4f1ec", padx=24)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(toolbar, text="Abrir CBR / CBZ", command=self.open_archive, style="Accent.TButton").pack(side="left")
        self.archive_label = tk.Label(toolbar, text="Ningún manga abierto", font=("Segoe UI", 9), bg="#f4f1ec", fg="#68645e", padx=12)
        self.archive_label.pack(side="left", fill="x", expand=True, anchor="w")
        ttk.Button(toolbar, text="Exportar CBZ", command=self.export_cbz).pack(side="right")

        body = tk.Frame(self.root, bg="#e8e4dd", padx=12, pady=12)
        body.pack(fill="both", expand=True, padx=24)
        self.canvas = tk.Canvas(body, bg="#262624", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._render_page())

        footer = tk.Frame(self.root, bg="#f4f1ec", padx=24, pady=14)
        footer.pack(fill="x")
        nav = tk.Frame(footer, bg="#f4f1ec")
        nav.pack(side="left")
        ttk.Button(nav, text="← Anterior", command=self.prev_page).pack(side="left")
        self.page_label = tk.Label(nav, text="Página —", font=("Segoe UI", 10, "bold"), bg="#f4f1ec", fg="#20201e", width=18)
        self.page_label.pack(side="left", padx=8)
        ttk.Button(nav, text="Siguiente →", command=self.next_page).pack(side="left")

        actions = tk.Frame(footer, bg="#f4f1ec")
        actions.pack(side="right")
        ttk.Button(actions, text="Volver a detectar", command=self.redetect).pack(side="left")
        progress_frame = tk.Frame(self.root, bg="#f4f1ec", padx=24)
        progress_frame.pack(fill="x", pady=(0, 10))
        self.progress_text = tk.Label(progress_frame, text="Listo", font=("Segoe UI", 9), bg="#f4f1ec", fg="#68645e", anchor="w")
        self.progress_text.pack(fill="x", pady=(0, 4))
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100, value=0)
        self.progress.pack(fill="x")
        self.status = tk.Label(self.root, text="Abre un manga; las viñetas se detectarán automáticamente.", font=("Segoe UI", 9), bg="#f4f1ec", fg="#68645e", padx=24)
        self.status.pack(fill="x", pady=(0, 14))

    def _set_progress(self, label: str, current: int, total: int) -> None:
        percent = 100 if total <= 0 else max(0, min(100, round(current * 100 / total)))
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=percent)
        self.progress_text.config(text=f"{label} · {percent}%")
        self.root.update_idletasks()

    def _start_busy_progress(self, label: str) -> None:
        self.progress.stop()
        self.progress.configure(mode="indeterminate", maximum=100, value=0)
        self.progress_text.config(text=label)
        self.progress.start(12)
        self.root.update_idletasks()

    def _finish_progress(self, label: str = "Listo") -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        self.progress_text.config(text=label)
        self.root.update_idletasks()

    def _extract_cbr(self, extractor: str, path: Path, destination: Path) -> None:
        args = [extractor, "x", "-y", "-bsp0", f"-o{destination}", str(path)]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags)
            started = time.monotonic()
            while process.poll() is None:
                if time.monotonic() - started > 180:
                    process.kill()
                    process.wait()
                    raise TimeoutError("La extracción del CBR superó los 3 minutos.")
                self.progress.step(4)
                self.root.update_idletasks()
                time.sleep(0.05)
            if process.returncode:
                log.seek(0)
                detail = log.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or "No se pudo extraer el CBR.")

    def open_archive(self, filename: str | None = None) -> None:
        if filename is None:
            filename = filedialog.askopenfilename(title="Abrir manga", filetypes=[("Cómics", "*.cbz *.cbr"), ("Todos los archivos", "*.*")])
        if not filename:
            return
        path = Path(filename)
        self._set_progress("Preparando importación", 0, 1)
        try:
            if self.temp_dir:
                self.temp_dir.cleanup()
            self.temp_dir = create_extraction_tempdir()
            destination = Path(self.temp_dir.name)
            if path.suffix.lower() == ".cbz":
                with zipfile.ZipFile(path) as archive:
                    members = [member for member in archive.infolist()
                               if Path(member.filename).suffix.lower() in SUPPORTED_IMAGES and not member.is_dir()]
                    if not members:
                        raise ValueError("No se encontraron imágenes en el archivo.")
                    for image_index, member in enumerate(members):
                        # Use a flat safe filename; archive paths are never written to disk.
                        output = destination / f"{image_index:05d}{Path(member.filename).suffix.lower()}"
                        with archive.open(member) as src, output.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        self._set_progress("Importando CBZ", image_index + 1, len(members))
            elif path.suffix.lower() == ".cbr":
                extractor = find_7zip()
                if not extractor:
                    self._finish_progress("No se encontró 7-Zip")
                    messagebox.showerror("Falta 7-Zip", "Para abrir CBR instala 7-Zip y vuelve a intentarlo. Los CBZ se abren sin programas adicionales.")
                    return
                self._start_busy_progress("Extrayendo CBR con 7-Zip…")
                self._extract_cbr(extractor, path, destination)
            else:
                raise ValueError("Elige un archivo CBR o CBZ.")

            self.pages = sorted((p for p in destination.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES), key=lambda p: natural_key(p.name))
            if not self.pages:
                raise ValueError("No se encontraron imágenes en el archivo.")
            self.archive = path
            self.page_index = 0
            self.crops.clear()
            panel_count = 0
            self._set_progress("Analizando páginas", 0, len(self.pages))
            for index, page_path in enumerate(self.pages):
                with Image.open(page_path) as img:
                    page = ImageOps.exif_transpose(img).convert("RGB")
                    self.crops[index] = detect_panels(page)
                    panel_count += len(self.crops[index])
                self._set_progress("Analizando páginas", index + 1, len(self.pages))
            self.archive_label.config(text=path.name)
            self.status.config(text=f"Detección automática: {panel_count} paneles en {len(self.pages)} páginas.")
            self._render_page()
            self._finish_progress(f"Importación lista · {len(self.pages)} páginas")
        except Exception as exc:
            self._finish_progress("Error durante la importación")
            messagebox.showerror("No se pudo abrir", str(exc))

    def _render_page(self) -> None:
        self.canvas.delete("all")
        if not self.pages:
            self.page_label.config(text="Página —")
            return
        path = self.pages[self.page_index]
        try:
            with Image.open(path) as img:
                self.source_image = ImageOps.exif_transpose(img).convert("RGB")
        except Exception as exc:
            self.status.config(text=f"No se pudo leer {path.name}: {exc}")
            return
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        iw, ih = self.source_image.size
        self.display_scale = min((cw - 20) / iw, (ch - 20) / ih)
        dw, dh = max(1, round(iw * self.display_scale)), max(1, round(ih * self.display_scale))
        self.display_offset = ((cw - dw) // 2, (ch - dh) // 2)
        preview = self.source_image.resize((dw, dh), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(preview)
        ox, oy = self.display_offset
        self.canvas.create_image(ox, oy, anchor="nw", image=self.photo, tags="page")
        for number, box in enumerate(self.crops.get(self.page_index, []), 1):
            x1, y1, x2, y2 = self._to_canvas(box)
            self.canvas.create_rectangle(x1, y1, x2, y2, outline="#fa6848", width=2, tags="crop")
            self.canvas.create_oval(x1 + 4, y1 + 4, x1 + 26, y1 + 26, fill="#fa6848", outline="", tags="crop")
            self.canvas.create_text(x1 + 15, y1 + 15, text=str(number), fill="white", font=("Segoe UI", 10, "bold"), tags="crop")
        self.page_label.config(text=f"Página {self.page_index + 1} / {len(self.pages)}")

    def _to_canvas(self, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        ox, oy = self.display_offset
        x1, y1, x2, y2 = box
        s = self.display_scale
        return ox + x1 * s, oy + y1 * s, ox + x2 * s, oy + y2 * s

    def redetect(self) -> None:
        if not self.pages:
            return
        total = 0
        self._set_progress("Volviendo a detectar paneles", 0, len(self.pages))
        for index, path in enumerate(self.pages):
            with Image.open(path) as img:
                panels = detect_panels(ImageOps.exif_transpose(img).convert("RGB"))
            self.crops[index] = panels
            total += len(panels)
            self._set_progress("Volviendo a detectar paneles", index + 1, len(self.pages))
        self._render_page()
        self.status.config(text=f"Detección actualizada: {total} paneles en {len(self.pages)} páginas.")
        self._finish_progress(f"Detección lista · {total} paneles")

    def prev_page(self) -> None:
        if self.pages and self.page_index > 0:
            self.page_index -= 1
            self._render_page()

    def next_page(self) -> None:
        if self.pages and self.page_index < len(self.pages) - 1:
            self.page_index += 1
            self._render_page()

    def export_cbz(self) -> None:
        if not self.pages:
            messagebox.showinfo("Abre un manga", "Primero abre un archivo CBR o CBZ.")
            return
        panel_count = sum(len(items) for items in self.crops.values())
        if panel_count == 0:
            messagebox.showinfo("No hay paneles", "Arrastra sobre las viñetas que quieras exportar.")
            return
        default = (self.archive.stem if self.archive else "manga") + "_paneles.cbz"
        output = filedialog.asksaveasfilename(title="Guardar CBZ para KOReader", initialfile=default, defaultextension=".cbz", filetypes=[("Comic book ZIP", "*.cbz")])
        if not output:
            return
        try:
            boxes_by_page = [self.crops.get(page_no, []) for page_no in range(len(self.pages))]
            total_panels = sum(len(boxes) for boxes in boxes_by_page)
            self._set_progress("Dando formato para Kindle · 0 paneles", 0, total_panels)
            with tempfile.TemporaryDirectory(prefix="manga_formateado_") as formatted_dir:
                formatted_paths: list[Path] = []
                number = 0
                for page_no, path in enumerate(self.pages):
                    boxes = boxes_by_page[page_no]
                    if not boxes:
                        continue
                    with Image.open(path) as im:
                        page = ImageOps.exif_transpose(im).convert("RGB")
                        for box in boxes:
                            number += 1
                            crop = page.crop(tuple(round(v) for v in box))
                            panel = ImageOps.contain(crop, TARGET_SIZE, method=Image.Resampling.LANCZOS)
                            canvas = Image.new("RGB", TARGET_SIZE, "white")
                            canvas.paste(panel, ((TARGET_SIZE[0] - panel.width) // 2, (TARGET_SIZE[1] - panel.height) // 2))
                            formatted_path = Path(formatted_dir) / f"{number:04d}.jpg"
                            canvas.save(formatted_path, format="JPEG", quality=88, optimize=True)
                            formatted_paths.append(formatted_path)
                            self._set_progress(f"Dando formato para Kindle · {number}/{total_panels}", number, total_panels)

                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                    for index, formatted_path in enumerate(formatted_paths, 1):
                        archive.write(formatted_path, formatted_path.name)
                        self._set_progress(f"Creando archivo CBZ · {index}/{total_panels}", index, total_panels)
            self.status.config(text=f"Listo: {number} paneles · 600 × 800 px · CBZ para KOReader.")
            self._finish_progress(f"Exportación lista · {number} paneles")
            messagebox.showinfo("Exportación lista", f"Se guardaron {number} paneles en:\n{output}\n\nCopia el CBZ al Kindle y ábrelo con KOReader.")
        except Exception as exc:
            self._finish_progress("Error durante la exportación")
            messagebox.showerror("No se pudo exportar", str(exc))

    def _close(self) -> None:
        if self.temp_dir:
            self.temp_dir.cleanup()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = MangaPanelApp(root)
    if len(sys.argv) > 1:
        root.after_idle(lambda: app.open_archive(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
