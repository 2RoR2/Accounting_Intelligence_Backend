import type { SourceEvidence } from "@/types";

/** Only map explicit, bounded coordinates. Unknown units/origins are not guessed. */
export function imageEvidenceBox(item: SourceEvidence, width: number, height: number) {
  const box = item.bbox;
  if (!box || item.page !== 1 || box.coord_origin !== "TOPLEFT") return null;
  const size = item.page_size ?? (item.coordinate_units === "pixels" ? { width, height } : null);
  if (!size || !["pixels", "document_points"].includes(item.coordinate_units ?? "")) return null;
  if (![size.width, size.height, box.l, box.t, box.r, box.b].every(Number.isFinite)
    || size.width <= 0 || size.height <= 0 || width <= 0 || height <= 0
    || Math.abs(size.width / size.height - width / height) > 0.01
    || box.l < 0 || box.t < 0 || box.r > size.width || box.b > size.height
    || box.r <= box.l || box.b <= box.t) return null;
  return { left: `${box.l / size.width * 100}%`, top: `${box.t / size.height * 100}%`,
    width: `${(box.r - box.l) / size.width * 100}%`, height: `${(box.b - box.t) / size.height * 100}%` };
}
