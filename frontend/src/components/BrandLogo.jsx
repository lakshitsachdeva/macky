export default function BrandLogo({
  className = '',
  alt = 'Custody',
  style,
}) {
  return (
    <img
      src="/recurdo-logo.svg"
      alt={alt}
      className={className}
      style={style}
    />
  )
}
