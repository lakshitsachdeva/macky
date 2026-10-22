import { motion } from 'framer-motion'

export default function BlurText({
  text,
  delay = 80,
  animateBy = 'words',
  direction = 'top',
  className = '',
  onAnimationComplete,
}) {
  const parts = animateBy === 'letters' ? text.split('') : text.split(' ')
  const yFrom = direction === 'top' ? 18 : direction === 'bottom' ? -18 : 0

  return (
    <div className={className}>
      {parts.map((part, index) => (
        <motion.span
          key={`${part}-${index}`}
          initial={{
            opacity: 0,
            y: yFrom,
            filter: 'blur(12px)',
          }}
          animate={{
            opacity: 1,
            y: 0,
            filter: 'blur(0px)',
          }}
          transition={{
            duration: 0.55,
            ease: [0.16, 1, 0.3, 1],
            delay: (delay * index) / 1000,
          }}
          onAnimationComplete={
            index === parts.length - 1 ? onAnimationComplete : undefined
          }
          style={{ display: 'inline-block' }}
        >
          {part}
          {animateBy === 'words' && index < parts.length - 1 ? ' ' : ''}
        </motion.span>
      ))}
    </div>
  )
}
