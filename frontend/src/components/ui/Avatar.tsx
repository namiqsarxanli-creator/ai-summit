"use client";

import Image from "next/image";
import { useState } from "react";

import { isAvatarUrl, isLocalPreviewUrl } from "@/lib/avatar";

// The person's photo, or - when there is none, when the URL is not one the server
// makes, or when the image fails to load - the summit robot that has always been
// the portrait of everyone without one. The robot is the element's own CSS
// background, so there is never a moment with nothing in the circle: the photo
// simply covers it once it has loaded, and uncovers it again if it errors.
//
// Decorative (`aria-hidden`): the name is always written next to it.
export type AvatarSize = "rank" | "profile" | "card";

const CLASS: Record<AvatarSize, string> = {
  rank: "fm-avatar fm-rank-avatar shrink-0",
  profile: "fm-avatar fm-avatar-profile",
  card: "fm-avatar fm-avatar-card",
};

const SIZES: Record<AvatarSize, string> = { rank: "43px", profile: "72px", card: "88px" };

export function Avatar({
  url,
  previewUrl,
  size,
}: {
  url?: string | null;
  // A not-yet-uploaded photo chosen on this device (a blob: URL); shown instead of `url`.
  previewUrl?: string | null;
  size: AvatarSize;
}) {
  const src = isLocalPreviewUrl(previewUrl) ? previewUrl : isAvatarUrl(url) ? url : null;
  // The src that failed to load, not a boolean: a new photo (a new URL) is tried afresh.
  const [failed, setFailed] = useState<string | null>(null);
  const showImage = src !== null && failed !== src;

  return (
    <span className={CLASS[size]} aria-hidden="true" data-photo={showImage ? "true" : "false"}>
      {showImage && (
        <Image
          src={src}
          alt=""
          fill
          sizes={SIZES[size]}
          unoptimized
          draggable={false}
          className="fm-avatar-img"
          onError={() => setFailed(src)}
        />
      )}
    </span>
  );
}
