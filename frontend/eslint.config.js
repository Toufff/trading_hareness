// Minimal lint config (WP8 audit item G/F): eslint recommended + vue
// recommended + typescript-eslint recommended, plus no-unused-vars. No style
// rules, no project-specific customization beyond what's needed to type-check
// <script lang="ts"> blocks inside .vue files.
import tseslint from 'typescript-eslint';
import pluginVue from 'eslint-plugin-vue';
import vueParser from 'vue-eslint-parser';

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  ...pluginVue.configs['flat/recommended'],
  ...tseslint.configs.recommended,
  // typescript-eslint's recommended config sets languageOptions.parser for
  // every file (no `files` filter), which clobbers the vue-eslint-parser that
  // eslint-plugin-vue assigned above. Re-assert it here for .vue files, with
  // @typescript-eslint/parser as the sub-parser for their <script lang="ts">.
  {
    files: ['**/*.vue'],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    rules: {
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
);
