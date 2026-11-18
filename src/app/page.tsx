import { PublicPage } from "@/features/auth/public-pages";
import { Suspense } from "react";
import { AppLoader } from "@/components/common/feedback/app-loader";
import { OpeningLoader } from "@/components/common/feedback/opening-loader";
export default function Home() {
  return (
    <OpeningLoader>
      <Suspense fallback={<AppLoader />}>
        <PublicPage page="" />
      </Suspense>
    </OpeningLoader>
  );
}
