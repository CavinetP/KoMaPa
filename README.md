# KOMaPa

KOMaPa es una aplicación de escritorio para Windows que convierte mangas **CBR/CBZ** en un **CBZ optimizado para Kindle 10 y KOReader**.

Detecta las viñetas automáticamente y crea hasta **3 bloques de lectura por página**. Ajusta cada bloque a **600 × 800 píxeles** y muestra el progreso al importar, analizar y exportar.

Si no encuentra cortes seguros, intenta dividir la página en dos bloques; si tampoco puede hacerlo sin riesgo, conserva la página completa para evitar cortar dibujos o texto.

## Requisitos

- Windows 10 u 11
- Python 3.10 o posterior
- Pillow (`python -m pip install -r requirements.txt`)
- Para archivos CBR: 7-Zip instalado. Los archivos CBZ no requieren 7-Zip.

## Iniciar

1. Descomprime esta carpeta en el PC.
2. Abre una terminal dentro de la carpeta y ejecuta `python -m pip install -r requirements.txt`.
3. Ejecuta `python app.py`.
4. Pulsa **Abrir CBR / CBZ**. La app analiza las páginas y muestra los límites detectados y el orden de lectura.
5. Pulsa **Exportar CBZ** y guarda el archivo.
6. Copia el CBZ resultante al Kindle por USB. En KOReader, ábrelo desde el explorador de archivos.

Los recuadros numerados muestran la detección en orden de lectura. **Volver a detectar** repite el análisis si es necesario. Cada hoja produce como máximo tres bloques. Si no se pueden detectar paneles individuales con seguridad, la app busca una separación clara y estrecha para dividir la hoja en dos bloques; si tampoco encuentra una división segura, conserva la página completa para evitar cortar contenido.

## Formatos y límites

- CBZ se extrae directamente.
- CBR necesita 7-Zip disponible en el PATH o en su ubicación habitual de instalación.
- La detección automática busca calles claras y líneas divisorias oscuras. Los recortes se solapan sobre la calle detectada para conservar los trazos del marco en ambos lados.
- Evita cortar bandas cortas de títulos y descarta divisiones que dejarían fragmentos demasiado pequeños, para mantener palabras y letras dentro de su panel.
- Une automáticamente una franja horizontal muy baja con el panel contiguo cuando ambos comparten casi todo el ancho; así evita separar como panel una pequeña banda superior o inferior.
- Junta hasta tres paneles pequeños que estén alineados en la misma fila. Si una separación muestra indicios de que un dibujo la cruza, intenta dividir la hoja en dos bloques usando una calle horizontal o vertical clara; si no encuentra una, conserva la página completa.
- Evita tomar como división una franja blanca dentro del margen y conserva las viñetas pequeñas cuando sus bordes negros confirman que son paneles independientes.
- Cada panel se ajusta dentro de una página blanca de 600 × 800 sin deformar la imagen.
- El proyecto no cambia ni instala firmware en el Kindle.
