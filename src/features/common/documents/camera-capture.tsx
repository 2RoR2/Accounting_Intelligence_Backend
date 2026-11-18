"use client";

import { useEffect, useRef, useState } from "react";
import { Camera, Check, RotateCcw, X } from "lucide-react";

type Ratio = "free" | "invoice" | "bill" | "receipt";
type Crop = { x: number; y: number; w: number; h: number };
type Corner = "move" | "nw" | "ne" | "sw" | "se";

const RATIOS: Record<Ratio, number | null> = {
  free: null,
  invoice: 210 / 297,
  bill: 1 / 1.35,
  receipt: 1 / 2.4,
};

function clampCrop(crop: Crop, bounds: { w: number; h: number }, ratio: number | null): Crop {
  let w = Math.max(64, crop.w);
  let h = Math.max(64, crop.h);
  if (ratio) {
    h = w / ratio;
    if (h > bounds.h) {
      h = bounds.h;
      w = h * ratio;
    }
  }
  w = Math.min(w, bounds.w);
  h = Math.min(h, bounds.h);
  if (ratio) h = Math.min(bounds.h, w / ratio);
  const x = Math.max(0, Math.min(crop.x, bounds.w - w));
  const y = Math.max(0, Math.min(crop.y, bounds.h - h));
  return { x, y, w, h };
}

export function CameraCapture({
  onConfirm,
  onClose,
}: {
  onConfirm: (file: File) => void;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [facing, setFacing] = useState<"environment" | "user">("environment");
  const [shot, setShot] = useState("");
  const [error, setError] = useState("");
  const [ratio, setRatio] = useState<Ratio>("invoice");
  const [bounds, setBounds] = useState({ w: 0, h: 0 });
  const [crop, setCrop] = useState<Crop>({ x: 0, y: 0, w: 0, h: 0 });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function open() {
      setError("");
      streamRef.current?.getTracks().forEach((track) => track.stop());
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { ideal: facing } },
        });
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
        }
      } catch {
        if (!cancelled) {
          setError("The camera could not be opened. Allow camera access, or upload from the device.");
        }
      }
    }
    if (!shot) void open();
    return () => {
      cancelled = true;
    };
  }, [facing, shot]);

  useEffect(() => {
    return () => streamRef.current?.getTracks().forEach((track) => track.stop());
  }, []);

  function placeCrop(image: HTMLImageElement, nextRatio: Ratio) {
    const nextBounds = { w: image.clientWidth, h: image.clientHeight };
    const locked = RATIOS[nextRatio];
    let w = nextBounds.w * 0.82;
    let h = locked ? w / locked : nextBounds.h * 0.82;
    if (h > nextBounds.h * 0.92) {
      h = nextBounds.h * 0.92;
      w = locked ? h * locked : nextBounds.w * 0.82;
    }
    setBounds(nextBounds);
    setCrop(
      clampCrop(
        { x: (nextBounds.w - w) / 2, y: (nextBounds.h - h) / 2, w, h },
        nextBounds,
        locked,
      ),
    );
  }

  function capture() {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    streamRef.current?.getTracks().forEach((track) => track.stop());
    setShot(canvas.toDataURL("image/jpeg", 0.92));
  }

  function moveCrop(event: React.PointerEvent, corner: Corner) {
    if (!bounds.w) return;
    event.preventDefault();
    event.stopPropagation();
    const origin = { corner, x: event.clientX, y: event.clientY, crop };
    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - origin.x;
      const dy = ev.clientY - origin.y;
      const start = origin.crop;
      const locked = RATIOS[ratio];
      let next = { ...start };
      if (corner === "move") {
        next = { ...start, x: start.x + dx, y: start.y + dy };
      } else {
        const growX = corner === "se" || corner === "ne" ? dx : -dx;
        const growY = corner === "se" || corner === "sw" ? dy : -dy;
        next.w = start.w + growX;
        next.h = locked ? next.w / locked : start.h + growY;
        if (corner === "nw" || corner === "sw") next.x = start.x + start.w - next.w;
        if (corner === "nw" || corner === "ne") next.y = start.y + start.h - next.h;
      }
      setCrop(clampCrop(next, bounds, locked));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function confirm() {
    const image = imageRef.current;
    if (!image || !crop.w || !crop.h) return;
    setBusy(true);
    const scaleX = image.naturalWidth / image.clientWidth;
    const scaleY = image.naturalHeight / image.clientHeight;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(crop.w * scaleX));
    canvas.height = Math.max(1, Math.round(crop.h * scaleY));
    canvas
      .getContext("2d")
      ?.drawImage(
        image,
        crop.x * scaleX,
        crop.y * scaleY,
        canvas.width,
        canvas.height,
        0,
        0,
        canvas.width,
        canvas.height,
      );
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          setBusy(false);
          setError("The crop could not be saved. Try again.");
          return;
        }
        const file = new File([blob], `scan-${Date.now()}.jpg`, { type: "image/jpeg" });
        onConfirm(file);
      },
      "image/jpeg",
      0.92,
    );
  }

  return (
    <div className="scan-panel">
      <div className="scan-toolbar">
        <strong>{shot ? "Crop the document" : "Scan with the camera"}</strong>
        <button type="button" className="text-link" onClick={onClose}>
          Close
        </button>
      </div>
      {error && <p className="scan-error">{error}</p>}
      {!shot ? (
        <>
          <div className="scan-view">
            <video ref={videoRef} autoPlay playsInline muted />
          </div>
          <p className="scan-note">Point the camera at an invoice, bill, or receipt, then capture.</p>
          <div className="scan-actions">
            <button type="button" className="btn" onClick={() => setFacing((side) => (side === "environment" ? "user" : "environment"))}>
              <RotateCcw size={16} /> Flip camera
            </button>
            <button type="button" className="btn primary" onClick={capture} disabled={!!error}>
              <Camera size={16} /> Capture
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="scan-ratios" role="group" aria-label="Crop ratio">
            {(
              [
                ["free", "Free"],
                ["invoice", "Invoice"],
                ["bill", "Bill"],
                ["receipt", "Receipt"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={ratio === key ? "active" : ""}
                onClick={() => {
                  setRatio(key);
                  if (imageRef.current) placeCrop(imageRef.current, key);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="scan-stage">
            {/* Captured frame is a data URL from the camera canvas. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              ref={imageRef}
              src={shot}
              alt="Captured document"
              onLoad={(event) => placeCrop(event.currentTarget, ratio)}
            />
            {crop.w > 0 && (
              <div
                className="scan-crop"
                style={{ left: crop.x, top: crop.y, width: crop.w, height: crop.h }}
                onPointerDown={(event) => moveCrop(event, "move")}
              >
                {(["nw", "ne", "sw", "se"] as const).map((corner) => (
                  <i
                    key={corner}
                    className={`scan-handle ${corner}`}
                    onPointerDown={(event) => moveCrop(event, corner)}
                  />
                ))}
              </div>
            )}
          </div>
          <p className="scan-note">Drag the frame or a corner. Then confirm the crop.</p>
          <div className="scan-actions">
            <button
              type="button"
              className="btn"
              onClick={() => {
                setShot("");
                setCrop({ x: 0, y: 0, w: 0, h: 0 });
              }}
            >
              <X size={16} /> Retake
            </button>
            <button type="button" className="btn primary" onClick={confirm} disabled={busy}>
              <Check size={16} /> {busy ? "Saving…" : "Confirm"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
