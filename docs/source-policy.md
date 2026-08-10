# Публичные источники и политика запросов

Запуск использует только чтение из трёх открытых каталогов Product Opener:

| Source | Base URL | Формат | Лицензия |
| --- | --- | --- | --- |
| Open Food Facts | `https://world.openfoodfacts.org` | JSON, `v2/search` | ODbL-1.0 / Database Contents License |
| Open Beauty Facts | `https://world.openbeautyfacts.org` | JSON, `v2/search` | ODbL-1.0 / Database Contents License |
| Open Pet Food Facts | `https://world.openpetfoodfacts.org` | JSON, `v2/search` | ODbL-1.0 / Database Contents License |

Официальная документация перечисляет эти домены как отдельные продуктовые
каталоги с одинаковой API-структурой. Для чтения authentication не требуется,
но обязателен понятный User-Agent. Запуск использует
`supplier-data-pipeline/0.2 (github.com/sergey-lastochkin/supplier-data-pipeline)`.

Для `v2/search` опубликован предел 10 запросов в минуту на IP. Скрипт делает
по одному запросу на каталог и выдерживает 7 секунд между источниками.
Адаптер при самостоятельном многостраничном использовании также задерживает
следующий запрос минимум на 7 секунд. При 429/5xx безопасные read-запросы
повторяются с ограниченным exponential backoff.

Источник данных, ограничения и лицензии проверены по официальным материалам:

- [Open Food Facts API](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/)
- [Семейство Food, Beauty, Pet Food и Products](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorials/scanning-barcodes/)
- [Условия лицензирования](https://openfoodfacts.github.io/openfoodfacts-server/api/tutorials/license-be-on-the-legal-side/)

Это добровольно заполненные каталоги. Полнота, корректность и наличие полей не
гарантированы поставщиком API, поэтому схемный gate и пропуск неполных записей
являются частью сценария, а не исправлением данных догадками.
