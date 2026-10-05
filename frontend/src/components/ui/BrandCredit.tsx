"use client";

import Image from "next/image";

export function BrandCredit() {
  return (
    <footer className="fm-credit">
      <span>Powered by</span>
      <a href="https://aiworks.az/" target="_blank" rel="noopener noreferrer">
        <Image src="/aiworks-logo.png" alt="aiworks" width={98} height={43} />
      </a>
    </footer>
  );
}