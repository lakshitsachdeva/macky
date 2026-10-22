import { motion } from 'framer-motion'

function buildGradient(color, x, y, size) {
  return {
    position: 'absolute',
    left: `${x}%`,
    top: `${y}%`,
    width: `${size}px`,
    height: `${size}px`,
    borderRadius: '50%',
    background: `radial-gradient(circle, ${color} 0%, rgba(0,0,0,0) 70%)`,
    filter: 'blur(38px)',
    transform: 'translate(-50%, -50%)',
    mixBlendMode: 'screen',
    opacity: 0.7,
  }
}

export default function Aurora({
  colorStops = ['#1a0533', '#2d0a6e', '#0d1f6e', '#0a0a1a'],
  amplitude = 1,
  blend = 0.6,
  speed = 0.4,
}) {
  const duration = Math.max(10, 28 / Math.max(speed, 0.1))
  const alpha = Math.max(0.18, Math.min(0.75, blend))

  return (
    <div
      aria-hidden="true"
      style={{
        position: 'absolute',
        inset: 0,
        overflow: 'hidden',
        background:
          'radial-gradient(circle at 50% 40%, rgba(28,28,55,0.35), transparent 55%), #05050b',
      }}
    >
      {colorStops.map((color, index) => (
        <motion.div
          key={`${color}-${index}`}
          style={{
            ...buildGradient(
              color.replace(')', `, ${alpha})`).includes('rgb')
                ? color
                : color,
              15 + index * 25,
              28 + (index % 2) * 26,
              340 + index * 80,
            ),
          }}
          animate={{
            x: [
              0,
              (index % 2 === 0 ? 1 : -1) * 65 * amplitude,
              (index % 2 === 0 ? -1 : 1) * 35 * amplitude,
              0,
            ],
            y: [
              0,
              (index % 2 === 0 ? -1 : 1) * 40 * amplitude,
              (index % 2 === 0 ? 1 : -1) * 30 * amplitude,
              0,
            ],
            scale: [1, 1.1, 0.95, 1],
          }}
          transition={{
            duration: duration + index * 1.5,
            ease: 'easeInOut',
            repeat: Number.POSITIVE_INFINITY,
          }}
        />
      ))}

      <div
        style={{
          position: 'absolute',
          inset: 0,
          background:
            'linear-gradient(180deg, rgba(4,4,10,0.15), rgba(4,4,10,0.45) 50%, rgba(4,4,10,0.92))',
        }}
      />
    </div>
  )
}
