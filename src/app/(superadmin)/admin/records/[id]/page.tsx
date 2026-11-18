import { Application } from "@/features/application";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Application path={["admin", "records", id]} />;
}
