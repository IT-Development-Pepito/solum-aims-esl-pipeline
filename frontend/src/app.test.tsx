import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import App from "./App"

describe("App", () => {
  it("shows the ESL operations application title", () => {
    render(<App />)

    expect(
      screen.getByRole("heading", { name: "ESL Operations" }),
    ).toBeInTheDocument()
  })
})
