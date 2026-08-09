// Illustrative configuration-mapped import endpoint. Queue/repository helpers
// are target-configuration contracts, so this example is not standalone.
Функция ПринятьПакет(ТелоJSON) Экспорт
    ЧтениеJSON = Новый ЧтениеJSON;
    ЧтениеJSON.УстановитьСтроку(ТелоJSON);
    Данные = ПрочитатьJSON(ЧтениеJSON);
    Если ПакетУжеИмпортирован(Данные.batch_id) Тогда Возврат Новый Структура("status", "duplicate"); КонецЕсли;
    Для Каждого СтрокаТовара Из Данные.items Цикл
        Если СтрокаТовара.Свойство("ambiguous") И СтрокаТовара.ambiguous Тогда
            ДобавитьВОчерёдьРазбора(СтрокаТовара);
        Иначе
            ЗаписатьВоВременныйИмпорт(Данные.batch_id, СтрокаТовара);
        КонецЕсли;
    КонецЦикла;
    ЗафиксироватьПакет(Данные.batch_id);
    Возврат Новый Структура("status", "accepted");
КонецФункции
