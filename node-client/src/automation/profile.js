/**
 * profile.js
 * UserBillingProfile — replace placeholder values with real data or load
 * from environment variables so credentials never live in source code.
 */

export const UserBillingProfile = {
  // --- Personal ---
  firstName:  process.env.PROFILE_FIRST_NAME  ?? 'Ankur',
  lastName:   process.env.PROFILE_LAST_NAME   ?? 'Vashishtha',
  email:      process.env.PROFILE_EMAIL       ?? 'your@email.com',
  phone:      process.env.PROFILE_PHONE       ?? '9999999999',

  // --- Billing address ---
  address:    process.env.PROFILE_ADDRESS     ?? '123 Main Street',
  city:       process.env.PROFILE_CITY        ?? 'Mumbai',
  state:      process.env.PROFILE_STATE       ?? 'Maharashtra',
  zip:        process.env.PROFILE_ZIP         ?? '400001',
  country:    process.env.PROFILE_COUNTRY     ?? 'IN',

  // --- Payment (never hardcode real card numbers) ---
  // Load from env only; defaults are obviously invalid test values.
  cardNumber: process.env.CARD_NUMBER         ?? '',
  cardExpiry: process.env.CARD_EXPIRY         ?? '',  // MM/YY
  cardCvv:    process.env.CARD_CVV            ?? '',
  cardName:   process.env.CARD_NAME           ??
    `${process.env.PROFILE_FIRST_NAME ?? 'Ankur'} ${process.env.PROFILE_LAST_NAME ?? 'Vashishtha'}`,
};
