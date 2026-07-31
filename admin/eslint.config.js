// Линт-гейт хуков. Единственная задача этого конфига — класс падений
// «весь экран в белый лист»: нарушение порядка хуков (hook после раннего
// return / в условии) валит всё React-дерево в рантайме, а tsc его не
// видит. Ровно такой баг жил на проде 3.5 недели (Plans.tsx, фикс
// 94b7ee5) — с этим гейтом CI поймал бы его за секунду.
//
// Прочие стилевые правила намеренно НЕ включены: типы держит tsc в
// `npm run build`, а стилевой шум девальвировал бы красный статус линта.
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  { ignores: ["dist/**"] },
  {
    files: ["src/**/*.{ts,tsx}"],
    // Точечные eslint-disable в коде оставлены как документация намерения;
    // их «неиспользуемость» при текущей версии плагина — не проблема.
    linterOptions: { reportUnusedDisableDirectives: "off" },
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
];
