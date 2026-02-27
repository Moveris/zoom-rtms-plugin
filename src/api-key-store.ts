import { validateApiKey } from "@moveris/shared";

export interface ApiKeyStore {
  set(accountId: string, apiKey: string): void;
  get(accountId: string): string | undefined;
  has(accountId: string): boolean;
}

export class InMemoryApiKeyStore implements ApiKeyStore {
  private keys = new Map<string, string>();

  set(accountId: string, apiKey: string): void {
    if (!validateApiKey(apiKey)) {
      throw new Error("Invalid Moveris API key format");
    }
    this.keys.set(accountId, apiKey);
  }

  get(accountId: string): string | undefined {
    return this.keys.get(accountId);
  }

  has(accountId: string): boolean {
    return this.keys.has(accountId);
  }
}
