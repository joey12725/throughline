import { describe, expect, it } from "vitest";
import { channelLabel } from "./channelLabel";

describe("channelLabel", () => {
  it("drops a leading plugin segment", () => {
    expect(channelLabel("health.oura.daily", "health")).toBe("oura.daily");
    expect(channelLabel("youtube.picks", "youtube_feed")).toBe("picks");
  });
  it("drops a trailing plugin segment so siblings stay distinct", () => {
    expect(channelLabel("transcripts.wave", "wave")).toBe("transcripts");
    expect(channelLabel("recordings.wave", "wave")).toBe("recordings");
  });
  it("keeps names the plugin does not explain", () => {
    expect(channelLabel("chat.sms", "beeper", ["chat.sms", "chat.discord"])).toBe("sms");
    expect(channelLabel("a.wave", "beeper", ["a.wave", "b.wave"])).toBe("a.wave");
    expect(channelLabel("visits", "native")).toBe("visits");
    expect(channelLabel("chat.sms", null)).toBe("chat.sms");
  });
});
