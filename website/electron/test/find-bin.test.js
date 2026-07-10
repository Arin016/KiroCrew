const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { findKiroclawBin } = require("../find-bin");

const HOME = "/mock/home";
const RESOURCES = "/mock/resources";
const DIRNAME = "/mock/electron";

const fakeOs = { homedir: () => HOME };

const only = (target) => ({
  accessSync: (p) => { if (p !== target) throw new Error("ENOENT"); },
  constants: { X_OK: fs.constants.X_OK },
});

const none = {
  accessSync: () => { throw new Error("ENOENT"); },
  constants: { X_OK: fs.constants.X_OK },
};

describe("findKiroclawBin", () => {
  it("returns bundled path when it exists", () => {
    const bundled = path.join(RESOURCES, "backend-dist", "kiroclaw-backend", "kiroclaw-backend");
    const fakeFs = only(bundled);
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, bundled);
  });

  it("returns bundled venv layout (backend-dist/.../bin/kiroclaw) when the flat PyInstaller exe is absent", () => {
    const venvLayout = path.join(RESOURCES, "backend-dist", "kiroclaw-backend", "bin", "kiroclaw");
    const fakeFs = only(venvLayout);
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, venvLayout);
  });

  it("prefers the flat PyInstaller exe over the venv-layout bin/kiroclaw", () => {
    const bundled = path.join(RESOURCES, "backend-dist", "kiroclaw-backend", "kiroclaw-backend");
    const venvLayout = path.join(RESOURCES, "backend-dist", "kiroclaw-backend", "bin", "kiroclaw");
    const fakeFs = {
      accessSync: (p) => { if (p !== bundled && p !== venvLayout) throw new Error("ENOENT"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, bundled);
  });

  it("returns ~/.toolbox/bin/kiroclaw when bundled paths don't exist", () => {
    const toolboxBin = path.join(HOME, ".toolbox", "bin", "kiroclaw");
    const fakeFs = only(toolboxBin);
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, toolboxBin);
  });

  it("returns ~/.local/bin/kiroclaw when bundled and toolbox paths don't exist", () => {
    const localBin = path.join(HOME, ".local", "bin", "kiroclaw");
    const fakeFs = only(localBin);
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, localBin);
  });

  it("returns ~/.kiroclaw-app/.venv/bin/kiroclaw when only venv binary exists", () => {
    const venvBin = path.join(HOME, ".kiroclaw-app", ".venv", "bin", "kiroclaw");
    const fakeFs = only(venvBin);
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, venvBin);
  });

  it("returns ../bin/kiroclaw relative to dirname when only that path exists", () => {
    const binPath = path.resolve(DIRNAME, "..", "bin", "kiroclaw");
    const fakeFs = only(binPath);
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, binPath);
  });

  it("falls back to bare 'kiroclaw' when no candidates are executable", () => {
    const result = findKiroclawBin(none, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, "kiroclaw");
  });

  it("returns first match when multiple candidates exist", () => {
    const bundled = path.join(RESOURCES, "backend-dist", "kiroclaw-backend", "kiroclaw-backend");
    const localBin = path.join(HOME, ".local", "bin", "kiroclaw");
    const fakeFs = {
      accessSync: (p) => { if (p !== bundled && p !== localBin) throw new Error("ENOENT"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, bundled);
  });

  it("handles resourcesPath being undefined", () => {
    const localBin = path.join(HOME, ".local", "bin", "kiroclaw");
    const fakeFs = only(localBin);
    const result = findKiroclawBin(fakeFs, fakeOs, path, undefined, DIRNAME);
    assert.equal(result, localBin);
  });

  it("resolves dirname-relative dev path correctly", () => {
    const devBin = path.resolve(DIRNAME, "backend-dist", "kiroclaw-backend", "kiroclaw-backend");
    const fakeFs = only(devBin);
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, devBin);
  });

  it("skips candidates that throw non-ENOENT errors (e.g. EACCES)", () => {
    const venvBin = path.join(HOME, ".kiroclaw-app", ".venv", "bin", "kiroclaw");
    const fakeFs = {
      accessSync: (p) => { if (p !== venvBin) throw new Error("EACCES"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    const result = findKiroclawBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, venvBin);
  });
});
