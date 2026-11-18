import { Application } from "@/features/application";

export default async function Page({ params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  return <Application path={["workspace", ...path]} />;
}
