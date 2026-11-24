import { forwardRef, type HTMLAttributes } from "react";
import clsx from "clsx";
import styles from "./ScrollArea.module.css";

export interface ScrollAreaProps extends HTMLAttributes<HTMLDivElement> {
  axis?: "y" | "x" | "both";
}

/** A scroll container with the app's thin, almost-monochrome scrollbars. */
export const ScrollArea = forwardRef<HTMLDivElement, ScrollAreaProps>(function ScrollArea(
  { axis = "y", className, children, ...rest },
  ref,
) {
  return (
    <div ref={ref} className={clsx(styles.area, styles[axis], className)} {...rest}>
      {children}
    </div>
  );
});
