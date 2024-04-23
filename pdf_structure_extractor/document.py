import requests
from functools import cached_property
import fitz
from pdf_structure_extractor.lines import Lines
from pdf_structure_extractor import utils, definitions
import pandas as pd


class Document:
    def __init__(self, document_url, raw_lines=None, lines=None):
        """
        Class representing an Emergency Appeal document, e.g. a final report.

        Parameters
        ----------
        document_url : string (required)
            Download URL for the document.

        raw_lines : pandas DataFrame (default=None)
            Lines extracted from the document. Used for testing and debugging to speed up the processing.
        """
        self.document_url = document_url
        self.raw_lines_input = raw_lines
        self.lines_input = lines

    @cached_property
    def raw_lines(self):
        """
        Extract lines from the appeal document using PyMuPDF.
        """
        if self.raw_lines_input is not None:
            return Lines(self.raw_lines_input)

        if not self.document_url:
            return None

        # Extract lines from the PDF
        data = []
        total_y = 0

        # Get the document content and open with fitz
        document_url = self.document_url
        document = requests.get(document_url)
        doc = fitz.open(stream=document.content, filetype='pdf')

        # Loop through pages and paragraphs
        for page_number, page_layout in enumerate(doc):

            # Get drawings to get text highlights
            coloured_drawings = [
                drawing
                for drawing in page_layout.get_drawings()
                if (drawing['fill'] != (0.0, 0.0, 0.0))
            ]
            page_images = page_layout.get_image_info()

            # Loop through blocks
            blocks = page_layout.get_text("dict", flags=11)["blocks"]
            for block_number, block in enumerate(blocks):
                for line_number, line in enumerate(block["lines"]):
                    spans = [span for span in line['spans'] if span['text'].strip()]
                    for span_number, span in enumerate(spans):

                        # Check if the text block is contained in a drawing
                        highlights = []
                        for drawing in coloured_drawings:
                            if utils.get_overlap(span['bbox'], drawing['rect']):
                                drawing['overlap'] = utils.get_overlap(span['bbox'], drawing['rect'])
                                highlights.append(drawing)

                        # Get largest overlap
                        highlight_color_hex = None
                        if highlights:
                            largest_highlight = max(highlights, key=lambda x: x['overlap'])
                            highlight_color = largest_highlight['fill']
                            if highlight_color:
                                highlight_color_hex = '#%02x%02x%02x' % (
                                    int(255*highlight_color[0]),
                                    int(255*highlight_color[1]),
                                    int(255*highlight_color[2])
                                )

                        # Check if the span is contained in any page images
                        contains_images = [img for img in page_images if utils.contains(img['bbox'], span['bbox'])]

                        # Append results
                        span['text'] = span['text'].replace('\r', '\n')
                        span['bold'] = ("black" in span['font'].lower()) or ("bold" in span['font'].lower())
                        span['color'] = "#%06x" % span['color']
                        span['highlight_color'] = highlight_color_hex
                        span['page_number'] = page_number
                        span['block_number'] = block_number
                        span['line_number'] = line_number
                        span['span_number'] = span_number
                        span['origin_x'] = span['origin'][0]
                        span['origin_y'] = span['origin'][1]
                        span['total_y'] = span['origin'][1]+total_y
                        span['img'] = bool(contains_images)
                        span['bbox_x1'] = span['bbox'][0]
                        span['bbox_y1'] = span['bbox'][1]
                        span['bbox_x2'] = span['bbox'][2]
                        span['bbox_y2'] = span['bbox'][3]
                        data.append(span)

            total_y += page_layout.rect.height

        return Lines(pd.DataFrame(data))

    @cached_property
    def lines(self):
        """
        Process the raw lines to get the document content.
        """
        if self.lines_input is not None:
            return Lines(self.lines_input)

        if self.raw_lines is None:
            return None

        lines = self.raw_lines.copy()

        # Merge inline texts # Add exclude_texts TODO
        lines = lines.merge_inline_text()

        # Sort lines by y of blocks
        lines = lines.sort_blocks_by_y()

        # Combine spans on same line with same styles
        lines = lines.combine_spans_same_style()

        # Combine bullet points and lines
        lines = lines.combine_bullet_spans()

        # Add text_base
        lines['text_base'] = lines['text']\
            .str.replace(r'[^A-Za-z0-9 ]+', ' ', regex=True)\
            .str.replace(' +', ' ', regex=True)\
            .str.lower()\
            .str.strip()

        # Remove photo blocks, page numbers, references
        lines = self.remove_photo_blocks(lines=lines)
        lines = self.remove_page_labels_references(lines=lines)
        lines = self.drop_all_repeating_headers_footers(lines=lines)

        # Have to run again in case repeating headers or footers were below or above the page labels or references
        lines = self.remove_page_labels_references(lines=lines)
        lines = self.drop_all_repeating_headers_footers(lines=lines)

        # Remove reference numbers
        lines = self.remove_reference_labels(lines=lines)

        # Remove date superscript (th, st, etc)
        lines = self.remove_date_superscripts(lines=lines)

        return lines

    def remove_photo_blocks(self, lines):
        """
        Remove blocks which look like photos from the document lines.
        """
        lines['block_page'] = lines['block_number'].astype(str)+'_'+lines['page_number'].astype(str)
        photo_blocks = lines.loc[lines['text'].astype(str).str.contains('Photo: '), 'block_page'].unique()
        lines = lines.loc[~lines['block_page'].isin(photo_blocks)].drop(columns=['block_page'])

        return lines

    def remove_page_labels_references(self, lines):
        """
        Remove page numbers from page headers and footers.
        Assumes headers and footers are the vertically highest and lowest elements on the page.
        """
        for option in ['headers', 'footers']:

            # For each page, get the order of the blocks by vertical y distance
            ordered_blocks_by_page = lines\
                .sort_values(
                    by=['page_number', 'origin_y'],
                    ascending=[True, (True if option == 'headers' else False)]
                )\
                .groupby('page_number')['block_number'].unique()

            # Loop through pages
            for page_number, block_numbers in ordered_blocks_by_page.items():
                page_lines = lines.loc[lines['page_number'] == page_number]

                # Loop through blocks and remove page labels and references
                for block_number in block_numbers:

                    block = page_lines.loc[page_lines['block_number'] == block_number]

                    # Check if the whole block is a page label or reference
                    # only for footers otherwise risk of dropping too much
                    if option == 'footers':
                        if block.is_page_label() or block.is_reference():
                            lines.drop(labels=block.index, inplace=True)
                            continue

                    # Loop through lines and remove page numbers and references
                    lines_page_label_or_reference = block.groupby(['line_number']).apply(
                        lambda block_lines:
                            block_lines.is_page_label() or block_lines.is_reference()
                    )
                    block_lines_to_drop = block.loc[
                        block['line_number'].isin(lines_page_label_or_reference[lines_page_label_or_reference].index)
                    ]
                    lines.drop(labels=block_lines_to_drop.index, inplace=True)
                    if lines_page_label_or_reference.all():
                        continue

                    break

        return lines

    def drop_all_repeating_headers_footers(self, lines):
        """
        Drop all repeating headers and footers.
        Run until there are no more repeating headers or footers.
        """
        # Drop header blocks
        while True:
            repeating_blocks = self.get_repeating_blocks(which='top', lines=lines)
            if repeating_blocks.empty:
                break
            lines = lines.drop(repeating_blocks['index'].explode())

        # Drop header lines
        while True:
            repeating_lines = self.get_repeating_lines(which='top', lines=lines)
            if repeating_lines.empty:
                break
            lines = lines.drop(repeating_lines['index'].explode())

        # Drop footer blocks
        while True:
            repeating_blocks = self.get_repeating_blocks(which='bottom', lines=lines)
            if repeating_blocks.empty:
                break
            lines = lines.drop(repeating_blocks['index'].explode())

        # Drop footer lines
        while True:
            repeating_lines = self.get_repeating_lines(which='bottom', lines=lines)
            if repeating_lines.empty:
                break
            lines = lines.drop(repeating_lines['index'].explode())

        return lines

    def get_repeating_blocks(self, which, lines):
        """
        Drop any repeating elements at the top or bottom of pages.
        """
        # Get spans in blocks at top of each page
        lines['page_block'] = lines['page_number'].astype(str)+'_'+lines['block_number'].astype(str)

        # Get the top and bottom blocks on each page
        if which == 'top':
            page_blocks = lines.loc[lines.groupby(['page_number'])['origin_y'].idxmin()]
        elif which == 'bottom':
            page_blocks = lines.loc[lines.groupby(['page_number'])['origin_y'].idxmax()]
        else:
            raise RuntimeError('Unrecognised value for "which", should be "top" or "bottom"')

        # Get repeating texts
        elements = lines.loc[lines['page_block'].isin(page_blocks['page_block'].unique())]
        elements = elements\
            .reset_index()\
            .groupby(['page_number'])\
            .agg({'text_base': lambda x: ' '.join(x), 'index': tuple})
        elements = elements.loc[elements['text_base'].astype(bool)]
        repeating_texts = elements\
            .groupby(['text_base'])\
            .filter(lambda x: len(x) > 2)

        # Don't remove lessons learned or challenges titles # TODO

        return repeating_texts

    def get_repeating_lines(self, which, lines):
        """
        """
        # Get the top and bottom lines on each page
        if which == 'top':
            page_lines = lines.loc[lines.groupby(['page_number'])['origin_y'].idxmin()]
        elif which == 'bottom':
            page_lines = lines.loc[lines.groupby(['page_number'])['origin_y'].idxmax()]
        else:
            raise RuntimeError('Unrecognised value for "which", should be "top" or "bottom"')

        # Get repeating texts
        page_lines = page_lines.loc[page_lines['text_base'].astype(bool)]
        repeating_texts = page_lines\
            .reset_index()\
            .groupby(['text_base'])\
            .filter(lambda x: len(x) > 2)

        # Don't remove lessons learned or challenges titles # TODO

        # Don't remove bullets
        repeating_texts = repeating_texts.loc[~(
            repeating_texts['text'].str.strip().isin(definitions.BULLETS)
        )]

        return repeating_texts

    def remove_reference_labels(self, lines):
        """
        Remove the small reference labels that are in text.
        Remove based on fontsize.
        """
        lines = lines.loc[~(
            (lines['size'] <= 7) &
            (lines['text_base'].astype(str).str.isdigit())
        )]

        return lines

    def remove_date_superscripts(self, lines):
        """
        Remove the small reference labels that are in text.
        Remove based on fontsize.
        """
        date_superscripts = ['th', 'st', 'nd']
        lines = lines.loc[~(
            (lines['size'] <= 7) &
            (lines['text_base'].astype(str).str.strip().isin(date_superscripts))
        )]

        return lines

    @cached_property
    def titles(self):
        return self.lines.titles

    @cached_property
    def headings(self):
        return self.lines.headings
