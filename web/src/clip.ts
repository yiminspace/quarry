import { t } from "./i18n";
import { toast } from "./store/toastStore";

/** Copy to the clipboard — only claims "Copied" when the write actually
 * succeeded (an honest failure toast otherwise). */
export function copy(v: string): void {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(String(v)).then(
      () => toast(t("copied"), true),
      () => toast(t("copy_fail"), false),
    );
  } else {
    toast(t("copy_fail"), false);
  }
}
