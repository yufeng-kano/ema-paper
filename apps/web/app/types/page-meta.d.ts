import type { AdminArea } from '~/lib/admin'

declare module '#app' {
  interface PageMeta {
    /** The area whose `read` opens a console page; the `admin` middleware and `AdminPage`
     *  check it (docs/admin.md § Pages). */
    adminArea?: AdminArea
  }
}
