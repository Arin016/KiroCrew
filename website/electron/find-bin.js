/**
 * Locate the kiroclaw backend binary by checking well-known paths in order.
 *
 * Returns the first executable candidate, or bare `"kiroclaw"` as a PATH
 * fallback. Dependencies are injected so the function is pure and testable
 * without mocking globals.
 *
 * @param {typeof import("fs")} fs - Node fs module (needs `accessSync`, `constants.X_OK`)
 * @param {typeof import("os")} os - Node os module (needs `homedir()`)
 * @param {typeof import("path")} path - Node path module
 * @param {string|undefined} resourcesPath - `process.resourcesPath` (Electron only)
 * @param {string} dirname - `__dirname` of the calling module
 * @returns {string} Absolute path to the binary, or `"kiroclaw"`
 */
function findKiroclawBin(fs, os, path, resourcesPath, dirname) {
  const home = os.homedir();
  const candidates = [
    // 1. Bundled PyInstaller binary (inside .app or dev electron/backend-dist)
    path.join(resourcesPath || "", "backend-dist", "kiroclaw-backend", "kiroclaw-backend"),
    path.resolve(dirname, "backend-dist", "kiroclaw-backend", "kiroclaw-backend"),
    path.resolve(dirname, "..", "bin", "kiroclaw"),
    // 2. Well-known install paths (toolbox, installer symlink, and venv)
    path.join(home, ".toolbox", "bin", "kiroclaw"),
    path.join(home, ".local", "bin", "kiroclaw"),
    path.join(home, ".kiroclaw-app", ".venv", "bin", "kiroclaw"),
  ];
  for (const bin of candidates) {
    try {
      fs.accessSync(bin, fs.constants.X_OK);
      return bin;
    } catch (e) {
      if (e.code !== "ENOENT") console.warn(`kiroclaw candidate ${bin}: ${e.code}`);
    }
  }
  return "kiroclaw"; // fall back to PATH
}

module.exports = { findKiroclawBin };
