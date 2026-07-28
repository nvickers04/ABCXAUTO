import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap font-semibold transition-colors duration-[var(--motion-quick)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:pointer-events-none disabled:opacity-40 active:scale-[0.98]",
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-fg hover:bg-primary-hover",
        secondary: "bg-fg text-bg hover:bg-fg/90",
        outline:
          "border border-border bg-transparent text-fg hover:bg-elevated",
        ghost: "bg-transparent text-fg hover:bg-elevated",
        danger: "bg-loss text-primary-fg hover:bg-loss/90",
        success: "bg-gain text-primary-fg hover:bg-gain/90",
      },
      size: {
        sm: "h-8 rounded-full px-3 text-xs",
        md: "h-10 rounded-full px-5 text-sm",
        lg: "h-12 rounded-full px-6 text-base",
        icon: "h-10 w-10 rounded-full p-0",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  ),
);
Button.displayName = "Button";
