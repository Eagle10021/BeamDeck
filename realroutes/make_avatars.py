#!/usr/bin/env python3
"""Passenger avatars for Rides from a folder of character art: <folder>/<Character>/<images>.

    python3 make_avatars.py "<folder>" [--out <data>/avatars]

For each character the art is searched for a face (nagadomi's lbpcascade_animeface, MIT; needs opencv), the clearest
one is cropped square round it and saved as <out>/<Character>.jpg (256 px). Only crops and resizes: nothing is drawn
or generated. An "@artist" in the source file's name is kept as the credit (avatars.json). The avatars are made from
the player's own files and stay in their data folder; they are never part of BeamDeck itself.
"""
import argparse, json, os, re, sys

STANDEE_W, STANDEE_H = 600, 1100        # the in-game cut-out card (about 0.9 m x 1.65 m)

CASCADE = os.path.expanduser("~/.cache/realroutes/lbpcascade_animeface.xml")
SKIP = re.compile(r"chibi|sticker|emote|icon", re.I)


def standee(cv2, img, face, out):
    """A tall card for the in-game cut-out: the face in the upper quarter, rounded corners, a white border."""
    import numpy as np
    x, y, fw, fh = face
    h, w = img.shape[:2]
    cw = min(w, fw * 3.4)
    ch = min(h, cw * STANDEE_H / STANDEE_W)
    cw = ch * STANDEE_W / STANDEE_H
    cx, top = x + fw / 2, y - fh * 0.9
    x0 = int(max(0, min(w - cw, cx - cw / 2)))
    y0 = int(max(0, min(h - ch, top)))
    crop = cv2.resize(img[y0:y0 + int(ch), x0:x0 + int(cw)], (STANDEE_W, STANDEE_H), interpolation=cv2.INTER_AREA)
    b = 14                                                     # white border
    card = np.full((STANDEE_H, STANDEE_W, 4), 255, np.uint8)
    card[b:-b, b:-b, :3] = cv2.resize(crop, (STANDEE_W - 2 * b, STANDEE_H - 2 * b))
    r = 46                                                     # rounded corners: transparent outside
    mask = np.zeros((STANDEE_H, STANDEE_W), np.uint8)
    cv2.rectangle(mask, (r, 0), (STANDEE_W - r, STANDEE_H), 255, -1)
    cv2.rectangle(mask, (0, r), (STANDEE_W, STANDEE_H - r), 255, -1)
    for cx2, cy2 in ((r, r), (STANDEE_W - r, r), (r, STANDEE_H - r), (STANDEE_W - r, STANDEE_H - r)):
        cv2.circle(mask, (cx2, cy2), r, 255, -1)
    card[:, :, 3] = mask
    cv2.imwrite(out, card)


def main():
    import cv2
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default=os.path.expanduser("~/.local/share/beamdeck/realroutes/avatars"))
    a = ap.parse_args()
    det = cv2.CascadeClassifier(CASCADE)
    if det.empty():
        sys.exit("face detector missing: " + CASCADE)
    os.makedirs(os.path.join(a.out, "standees"), exist_ok=True)
    credits = {}
    for name in sorted(os.listdir(a.folder)):
        d = os.path.join(a.folder, name)
        if not os.path.isdir(d):
            continue
        best = None
        for f in sorted(os.listdir(d)):
            if not f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) or SKIP.search(f):
                continue
            img = cv2.imread(os.path.join(d, f))
            if img is None:
                continue
            h, w = img.shape[:2]
            scale = 1600 / max(h, w)
            small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
            s = scale if scale < 1 else 1.0
            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            faces = det.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=6, minSize=(60, 60))
            for (x, y, fw, fh) in faces:
                # a big face, not right at an edge, scores best
                area = fw * fh / (small.shape[0] * small.shape[1])
                score = min(area, 0.06) - (0.05 if x < 5 or y < 5 else 0)
                if not best or score > best[0]:
                    best = (score, f, img, (x / s, y / s, fw / s, fh / s))
        if not best:
            # no face found: a square from the upper middle of the first usable picture
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and not SKIP.search(f):
                    img = cv2.imread(os.path.join(d, f))
                    if img is not None:
                        h, w = img.shape[:2]
                        fw = fh = min(h, w) * 0.26
                        best = (0, f, img, (w / 2 - fw / 2, h * 0.30 - fh / 2, fw, fh))
                        print(f"{name}: no face found, centred crop of {f}")
                        break
            if not best:
                continue
        _, f, img, (x, y, fw, fh) = best
        h, w = img.shape[:2]
        side = min(h, w, fw * 2.3)
        cx, cy = x + fw / 2, y + fh / 2 + fh * 0.18          # a little below the face's centre: some shoulders
        x0 = int(max(0, min(w - side, cx - side / 2)))
        y0 = int(max(0, min(h - side, cy - side / 2)))
        crop = img[y0:y0 + int(side), x0:x0 + int(side)]
        cv2.imwrite(os.path.join(a.out, f"{name}.jpg"), cv2.resize(crop, (256, 256), interpolation=cv2.INTER_AREA),
                    [cv2.IMWRITE_JPEG_QUALITY, 88])
        standee(cv2, img, (x, y, fw, fh), os.path.join(a.out, "standees", f"{name}.png"))
        m = re.search(r"@([\w.]+)", f)
        credits[name] = {"file": f, "artist": "@" + m.group(1) if m else None}
        print(f"{name}: {f}")
    json.dump(credits, open(os.path.join(a.out, "avatars.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
