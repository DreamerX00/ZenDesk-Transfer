/**
 * Minimal type shim for the ZAF SDK v2 client. The real SDK doesn't
 * ship its own types so we declare only the surface we actually use.
 */

declare global {
  interface Window {
    ZAFClient: {
      init(): ZAFClient;
    };
  }
}

export interface ZAFClient {
  on(event: string, handler: (...args: unknown[]) => void): void;
  off(event: string, handler: (...args: unknown[]) => void): void;
  get<T = unknown>(keys: string | string[]): Promise<T>;
  set(key: string, value: unknown): Promise<void>;
  invoke(method: string, ...args: unknown[]): Promise<unknown>;
  request(options: ZAFRequestOptions): Promise<unknown>;
  context(): Promise<ZAFContext>;
  metadata(): Promise<ZAFMetadata>;
  trigger(event: string, data?: unknown): Promise<void>;
}

export interface ZAFRequestOptions {
  url: string;
  type?: "GET" | "POST" | "PUT" | "DELETE" | "PATCH";
  data?: unknown;
  contentType?: string;
  headers?: Record<string, string>;
  secure?: boolean;
  cors?: boolean;
}

export interface ZAFContext {
  product: string;
  account: { subdomain: string };
  currentUser: { id: number; email: string; name: string };
  location: string;
  instanceGuid: string;
}

export interface ZAFMetadata {
  installationId: number;
  appId: number;
  name: string;
  version: string;
  settings: Record<string, string>;
}

export {};
