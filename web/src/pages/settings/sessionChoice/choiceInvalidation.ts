import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";

const PREVIEW_EPOCH = ["sessionChoicePreviewEpoch"] as const;

export function useChoicePreviewEpoch() {
  const client = useQueryClient();
  const { data: epoch } = useQuery({
    queryKey: PREVIEW_EPOCH,
    queryFn: () => 0,
    initialData: 0,
    enabled: false,
    gcTime: Infinity,
  });
  return {
    get current() {
      return client.getQueryData<number>(PREVIEW_EPOCH) ?? epoch;
    },
  };
}

export async function refreshChoiceConsumers(client: QueryClient) {
  // Invalidate in-flight previews before any reading refresh can fail or finish.
  client.setQueryData<number>(PREVIEW_EPOCH, (epoch = 0) => epoch + 1);
  await Promise.all(
    ["sessionChoiceFloors", "sessionChoiceMeters", "sessionChoiceLines", "sessionChoiceNotice"].map(
      (key) => client.invalidateQueries({ queryKey: [key] }, { throwOnError: true }),
    ),
  );
}
