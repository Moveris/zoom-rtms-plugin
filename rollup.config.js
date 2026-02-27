import resolve from "@rollup/plugin-node-resolve";
import commonjs from "@rollup/plugin-commonjs";
import terser from "@rollup/plugin-terser";

export default {
  input: "src/sidebar/public/sidebar.js",
  output: {
    file: "dist/sidebar/public/bundle.js",
    format: "iife",
    sourcemap: true,
  },
  plugins: [
    resolve({ browser: true }),
    commonjs(),
    terser(),
  ],
};
