export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const isMultipart = typeof FormData !== "undefined" && init?.body instanceof FormData;
  const response = await fetch("/api" + path, {
    ...init,
    headers: isMultipart ? { ...init?.headers } : { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `请求失败（${response.status}）`,
    );
  }
  return response.json();
}
