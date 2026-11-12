import { authenticatedFetch } from "@/lib/identity";

export async function readBoardJson<T>(path: string): Promise<T> {
  const response = await authenticatedFetch(path);
  if (!response.ok) throw new Error(`Task board read failed (${response.status})`);
  return response.json() as Promise<T>;
}
