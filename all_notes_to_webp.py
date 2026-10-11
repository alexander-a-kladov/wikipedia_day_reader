#!/usr/bin/python3

import os
import json

# Укажите путь к вашей папке с JSON-файлами
TARGET_DIRECTORY = "cache/notes/"

def convert_extensions_to_webp(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.json'):
                file_path = os.path.join(root, file)
                
                # Читаем JSON-файл
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    print(f"Ошибка чтения файла {file}: {e}")
                    continue

                # Рекурсивная функция для поиска и обновления ключа 'images'
                def update_images(obj):
                    if isinstance(obj, dict):
                        for key, value in obj.items():
                            if key == 'images' and isinstance(value, list):
                                # Меняем расширение для каждой строки в массиве
                                obj[key] = [
                                    os.path.splitext(img)[0] + '.webp' if isinstance(img, str) else img 
                                    for img in value
                                ]
                            else:
                                update_images(value)
                    elif isinstance(obj, list):
                        for item in obj:
                            update_images(item)

                # Запускаем обновление структуры данных
                update_images(data)

                # Перезаписываем файл с обновленными данными
                try:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=4)
                    print(f"Успешно обновлен: {file_path}")
                except Exception as e:
                    print(f"Ошибка записи файла {file}: {e}")

# Запуск скрипта
convert_extensions_to_webp(TARGET_DIRECTORY)

