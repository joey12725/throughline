/*
 * Eager drawer registration.
 *
 * URL-addressable drawers normally register as a side effect of the feature
 * that opens them. That leaves a cold deep-link — e.g. opening
 * `…/ui/sources?d=record:id=m:1549` on a screen that never imports the record
 * feature — showing "No panel registered" until a channel is visited.
 *
 * Importing the record feature here registers its drawer at app start. The
 * module itself is tiny: the record body (react-markdown and the overlay
 * queries) stays a lazy chunk and loads only when a record is first opened.
 */
import "@/features/record";
