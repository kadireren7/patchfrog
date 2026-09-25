// Stripe checkout for the web storefront.
import Stripe from "stripe";

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);

export async function createSession(items) {
  const session = await stripe.checkout.sessions.create({
    mode: "payment",
    line_items: items,
  });
  return session.url;
}

export async function refund(paymentIntent) {
  return stripe.refunds.create({ payment_intent: paymentIntent });
}
