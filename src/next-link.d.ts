declare module "next/link" {
  import type { ReactNode, AnchorHTMLAttributes } from "react";

  export default function Link(
    props: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children?: ReactNode },
  ): JSX.Element;
}
