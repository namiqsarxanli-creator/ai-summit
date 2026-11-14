// A yes/no question in the site's own dialog instead of the browser's grey box
// (owner's request, 8 Nov 2026). The dialog lives in the root layout
// (components/ui/ConfirmHost.tsx); a page rendered without it - a unit test,
// for one - still gets the browser's own confirm, so no question is ever skipped.

export type ConfirmRequest = { message: string; resolve: (ok: boolean) => void };

let host: ((request: ConfirmRequest) => void) | null = null;

export function registerConfirmHost(show: (request: ConfirmRequest) => void): () => void {
  host = show;
  return () => {
    if (host === show) host = null;
  };
}

/** Resolves true only when the person chose to go ahead. */
export function confirmAction(message: string): Promise<boolean> {
  const show = host;
  if (!show) return Promise.resolve(window.confirm(message));
  return new Promise((resolve) => show({ message, resolve }));
}
