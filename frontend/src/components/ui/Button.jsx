import React from 'react';

export function Button({
  children,
  variant = 'primary',
  className = '',
  ...props
}) {
  let btnClass = 'btn-primary';
  if (variant === 'outline')       btnClass = 'btn-outline';
  if (variant === 'outline-red')   btnClass = 'btn-outline-red';
  if (variant === 'outline-amber') btnClass = 'btn-outline-amber';

  return (
    <button className={`${btnClass} ${className}`} {...props}>
      {children}
    </button>
  );
}
