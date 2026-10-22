import { LegalPage } from './shared'

function TermsPage() {
  return (
    <LegalPage
      title="terms of use"
      lede="Recurdo is currently beta software. These terms are intentionally simple and focused on safe use."
      sections={[
        {
          title: 'As-is beta software',
          paragraphs: [
            'The app is provided as-is, without warranties of any kind. Features, behavior, and availability may change while the product is in beta.',
          ],
        },
        {
          title: 'Backups and data loss',
          paragraphs: [
            'You are responsible for keeping backups of your files. We are not responsible for data loss, corrupted files, or workflow interruptions.',
          ],
        },
        {
          title: 'Acceptable use',
          items: [
            'Do not use the app for illegal purposes.',
            'Do not attempt to abuse or reverse engineer hosted waitlist infrastructure.',
          ],
        },
        {
          title: 'Subject to change',
          paragraphs: [
            'These terms may be updated as the product evolves. Continued use of the waitlist or app after changes means you accept the updated terms.',
          ],
        },
      ]}
    />
  )
}

export default TermsPage
