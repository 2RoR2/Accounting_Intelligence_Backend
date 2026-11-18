import Image from "next/image";
import accordAiLogo from "@/images/AccordAI_logo.png";

export function AccountingLoader({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`accounting-loader${compact ? " accounting-loader-compact" : ""}`} role="img" aria-label="Loading Accounting Intelligence AccordAI">
      <div className="accounting-loader-mark" aria-hidden="true"><div className="accounting-loader-orb"><span /><span /><span /><span /><Image src={accordAiLogo} alt="" className="accounting-loader-logo" width={150} height={46} priority /></div></div>
    </div>
  );
}

export function AppLoader() {
  return (
    <main
      className="accordAi-loading-screen"
      aria-live="polite"
      aria-busy="true"
      aria-label="Preparing Accounting Intelligence workspace"
    >
      <div className="accordAi-loader-ambient" aria-hidden="true">
        <span className="accordAi-ambient-one" />
        <span className="accordAi-ambient-two" />
      </div>

      <div className="accordAi-loader-content">
        <AccountingLoader />

      </div>
    </main>
  );
}
