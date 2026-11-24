/* The design system. Feature authors import primitives from "@/ui". */
export { Button, IconButton } from "./Button";
export type { ButtonProps, IconButtonProps, ButtonVariant, ButtonSize } from "./Button";
export { Chip, StaticChip } from "./Chip";
export type { ChipProps } from "./Chip";
export { Badge, StatusGlyph } from "./Badge";
export type { BadgeProps, StatusGlyphProps } from "./Badge";
export { Tabs, Segmented } from "./Tabs";
export type { TabsProps, TabItem, SegmentedProps } from "./Tabs";
export { Tooltip } from "./Tooltip";
export type { TooltipProps } from "./Tooltip";
export { Popover, usePopover } from "./Popover";
export type { PopoverProps, PopoverControl } from "./Popover";
export { Menu } from "./Menu";
export type { MenuProps, MenuItem, MenuEntry } from "./Menu";
export { Dialog } from "./Dialog";
export type { DialogProps } from "./Dialog";
export { Drawer, DrawerHost } from "./Drawer";
export type { DrawerProps } from "./Drawer";
export {
  useDrawers,
  registerDrawer,
  getDrawerRenderer,
  encodeStack,
  decodeStack,
} from "./drawerStore";
export type { DrawerSpec, DrawerRenderProps } from "./drawerStore";
export { Toaster } from "./Toaster";
export { useToasts, toast } from "./toast";
export type { Toast, ToastTone } from "./toast";
export { Skeleton, EmptyState, ErrorState } from "./Feedback";
export type { EmptyStateProps, ErrorStateProps } from "./Feedback";
export { Stat, KeyValue } from "./Stat";
export type { StatProps, KeyValueRow } from "./Stat";
export { Kbd, cmdKey } from "./Kbd";
export { Field, Input, Select, Switch } from "./Form";
export type { FieldProps, InputProps, SelectProps, SwitchProps } from "./Form";
export { ScrollArea } from "./ScrollArea";
export type { ScrollAreaProps } from "./ScrollArea";
export { Split } from "./Split";
export type { SplitProps } from "./Split";
export { CodeBlock } from "./CodeBlock";
export type { CodeBlockProps } from "./CodeBlock";
export { RelativeTime } from "./RelativeTime";
export type { RelativeTimeProps } from "./RelativeTime";
export { Sensitive } from "./Sensitive";
export type { SensitiveProps } from "./Sensitive";
export { SourceDot, SourceChip } from "./Source";
export type { SourceDotProps, SourceChipProps } from "./Source";
export { Panel } from "./Panel";
export type { PanelProps } from "./Panel";
export { Page, PageHeader } from "./Page";
export type { PageProps, PageHeaderProps } from "./Page";

export { SourceColorProvider, useSourceColor } from "./SourceColorContext";
export {
  STATE,
  pluginState,
  type SourceState,
  type Tone,
  type StateDescriptor,
} from "./status";
export * as sourceColor from "./sourceColor";
export { channelLabel } from "./channelLabel";
