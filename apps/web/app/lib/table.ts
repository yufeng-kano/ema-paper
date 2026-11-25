/**
 * The column contract `UiDataTable` renders (see the component for what it does with each
 * field).
 */
export interface TableColumn<Row> {
  key: string
  /** Visible column header. Blank for an action column, which needs `srHeader` instead. */
  header: string
  /** Right-aligned under its header, with tabular numerals. */
  numeric?: boolean
  /** Right-aligns header and cells without the numeric treatment — for a control column. */
  align?: 'end'
  /** Plain-text value, for a column whose cell needs no markup of its own. */
  value?: (row: Row) => string
  /**
   * Track width. An action column needs one: without it the column takes the table's
   * leftover width and its control ends up a long way from the row it acts on.
   */
  width?: string
  /**
   * Accessible name for a column whose `header` is intentionally blank. A blank `<th>` is an
   * unnamed column to a screen reader reading the table's structure, so the name is
   * rendered visually hidden rather than omitted.
   */
  srHeader?: string
}
