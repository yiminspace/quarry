import { describe, expect, it } from "vitest";
import { decodeFocus, decodeQueryLink, encodeFocusSearch, encodeQueryLink, focusTitle } from "./queryLink";

describe("query link codec", () => {
  it("round-trips Chinese, newlines, quotes and symbols", () => {
    const original = {
      db: "shop",
      env: "prod",
      sql: "select '中文\"x\"'\nfrom orders where note like '%a&b=c%';",
    };
    const link = encodeQueryLink("http://localhost:9876/app/", original);
    const parsed = decodeQueryLink(new URL(link).search);
    expect(parsed).toEqual(original);
  });

  it("treats empty env as null", () => {
    const parsed = decodeQueryLink("?db=testpg&env=&sql=select%201");
    expect(parsed).toEqual({ db: "testpg", env: null, sql: "select 1" });
  });

  it("returns null when required params are missing", () => {
    expect(decodeQueryLink("?db=testpg")).toBeNull();
    expect(decodeQueryLink("?sql=select%201")).toBeNull();
  });
});

describe("focus URL", () => {
  it("encodes db/env/table and strips leftover sql", () => {
    expect(encodeFocusSearch({ db: "shop", env: "dev", table: "customers" })).toBe(
      "?db=shop&env=dev&table=customers",
    );
    expect(encodeFocusSearch({ db: null, env: null, table: null })).toBe("");
  });

  it("decodes a focus URL and ignores share links", () => {
    expect(decodeFocus("?db=shop&env=dev&table=customers")).toEqual({
      db: "shop",
      env: "dev",
      table: "customers",
    });
    expect(decodeFocus("?db=shop&sql=select%201")).toBeNull();
    expect(decodeFocus("?sql=select%201")).toBeNull();
  });

  it("builds a title the address bar can share with an agent", () => {
    expect(focusTitle({ db: null, env: null, table: null })).toBe("Quarry");
    expect(focusTitle({ db: "shop", env: "dev", table: null })).toBe("shop@dev");
    expect(focusTitle({ db: "shop", env: "dev", table: "customers" })).toBe("shop@dev · customers");
  });
});
