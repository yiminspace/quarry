import { describe, expect, it } from "vitest";
import { collectColumnImages, resultImageUrl } from "./imageDetection";

describe("result image candidates", () => {
  it.each(["png", "JPG", "jpeg", "webp", "gif", "avif", "bmp", "ico", "svg"])("accepts %s with query and fragment", (extension) => {
    expect(resultImageUrl(`https://images.example/a.${extension}?signature=abc%2Fxyz#fragment`)?.origin)
      .toBe("https://images.example");
  });

  it.each([null, 1, "http://example.com/a.png", "https:///a.png", "https://example.com/a", "https://example.com/?file=a.png", "https://example.com/?filename=notes.txt", "http://example.com/?filename=a.png",
    "file:///tmp/a.png", "data:image/png;base64,abc", "https://user:password@example.com/a.png"])("rejects %s", (value) => {
    expect(resultImageUrl(value)).toBeNull();
  });

  it("recognizes an image filename on an extensionless object URL and keeps its original parameters", () => {
    const url = "https://images.example/bucket/app-icon?filename=app%2EPNG&signature=a%2Fb";
    expect([...collectColumnImages([{ icon: url }], "icon")]).toEqual([url]);
  });

  it("reads only the selected column and returns distinct original URLs", () => {
    const first = "https://images.example/one.png?token=a%2Fb";
    const second = "https://images.example/one.png?token=b";
    const third = "https://other.example:8443/three.png";
    const row = { chosen: first, get other(): never { throw new Error("Unselected column read"); } };
    const images = collectColumnImages([row, row, { chosen: second }, { chosen: third }, { chosen: "text" }], "chosen");
    expect([...images]).toEqual([first, second, third]);
  });
});
