/**
 * Minimal typings for the slice of noVNC this app uses.
 *
 * @novnc/novnc ships no type declarations; only the documented RFB surface is
 * declared here so the integration stays type-checked.
 */
declare module "@novnc/novnc" {
  export interface RFBOptions {
    credentials?: { username?: string; password?: string };
    shared?: boolean;
    repeaterID?: string;
    wsProtocols?: string[];
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: RFBOptions);

    viewOnly: boolean;
    clipViewport: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    showDotCursor: boolean;
    qualityLevel: number;
    compressionLevel: number;
    background: string;
    capabilities: { power: boolean };

    disconnect(): void;
    focus(options?: { preventScroll?: boolean }): void;
    blur(): void;
    sendCtrlAltDel(): void;
    sendKey(keysym: number, code: string, down: boolean): void;
    clipboardPasteFrom(text: string): void;
  }
}
